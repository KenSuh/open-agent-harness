# SQL 플러그인 검사 — Postgres·MySQL·SQLite 식 마이그레이션에서 테이블·뷰·루틴·트리거·인덱스·GRANT 와 연결을 뽑고, DB 질의가 그래프 신원을 함께 내는지 본다.
from __future__ import annotations

import hashlib
import json
import pathlib
import unittest

from oah.impact import sqlplugin
from tests.helpers import git_commit, make_git_repo
from tests.test_impact_map import run_impact

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "impact" / "sql"


def load(dialect: str) -> dict[str, str]:
    folder = FIXTURES / dialect
    return {f"db/{path.name}": path.read_text(encoding="utf-8") for path in sorted(folder.glob("*.sql"))}


def line_of(text: str, marker: str) -> int:
    return next(number for number, line in enumerate(text.splitlines(), 1) if marker in line)


class ExtractTest(unittest.TestCase):
    def kinds(self, graph: dict) -> dict[str, str]:
        return {entity: info["kind"] for entity, info in graph["entities"].items()}

    def links(self, graph: dict) -> set[tuple[str, str, str]]:
        return {(source, target, relation) for source, target, relation, _file, _line in graph["links"]}

    def test_postgres_entities(self):
        graph = sqlplugin.extract(load("postgres"))
        self.assertEqual(self.kinds(graph), {
            "db_table:users": "table", "db_table:orders": "table", "db_table:purchases": "table",
            "db_table:order_totals": "view", "db_table:legacy_view": "view",
            "db_routine:touch_order": "function", "db_routine:orders_touch": "trigger",
            "db_grant:app_rw": "grant",
        })
        self.assertLessEqual({
            ("db_table:orders", "db_table:users", "foreign-key"),
            ("db_table:order_totals", "db_table:users", "view-source"),
            ("db_table:order_totals", "db_table:orders", "view-source"),
            ("db_routine:touch_order", "db_table:orders", "uses"),
            ("db_routine:orders_touch", "db_table:orders", "trigger-on"),
            ("db_routine:orders_touch", "db_routine:touch_order", "calls"),
            ("db_grant:app_rw", "db_table:orders", "grant:SELECT,INSERT"),
            ("db_grant:app_rw", "db_table:users", "grant:SELECT,INSERT"),
            ("db_grant:app_rw", "db_routine:touch_order", "grant:EXECUTE"),
            ("db_grant:app_rw", "db_table:users", "revoke:INSERT"),
        }, self.links(graph))
        self.assertEqual(graph["entities"]["db_table:orders"]["indexes"],
                         [["orders_user_idx", "db/001_init.sql", 11]])

    def test_mysql_entities(self):
        graph = sqlplugin.extract(load("mysql"))
        self.assertEqual(self.kinds(graph), {
            "db_table:customers": "table", "db_table:invoices": "table", "db_table:bills": "table",
            "db_table:invoice_view": "view", "db_routine:close_invoice": "procedure",
            "db_routine:invoices_ai": "trigger", "db_grant:report": "grant", "db_grant:admin": "grant",
        })
        self.assertLessEqual({
            ("db_table:invoices", "db_table:customers", "foreign-key"),
            ("db_table:invoice_view", "db_table:invoices", "view-source"),
            ("db_routine:close_invoice", "db_table:invoices", "uses"),
            ("db_routine:close_invoice", "db_table:customers", "uses"),
            ("db_routine:invoices_ai", "db_table:invoices", "trigger-on"),
            ("db_routine:invoices_ai", "db_table:customers", "uses"),
            ("db_grant:report", "db_table:customers", "grant:SELECT,UPDATE"),
            ("db_grant:admin", "db_schema:shop", "grant:ALL PRIVILEGES"),
        }, self.links(graph))
        self.assertEqual(graph["entities"]["db_table:invoices"]["indexes"],
                         [["idx_invoices_customer", "db/001_init.sql", 12]])

    def test_sqlite_entities(self):
        graph = sqlplugin.extract(load("sqlite"))
        self.assertEqual(self.kinds(graph), {
            "db_table:notes": "table", "db_table:authors": "table", "db_table:notes_fts": "table",
            "db_table:recent_notes": "view", "db_routine:notes_ai": "trigger", "db_table:old_notes": "table",
        })
        self.assertLessEqual({
            ("db_table:notes", "db_table:authors", "foreign-key"),
            ("db_table:recent_notes", "db_table:notes", "view-source"),
            ("db_routine:notes_ai", "db_table:notes", "trigger-on"),
            ("db_routine:notes_ai", "db_table:notes_fts", "uses"),
        }, self.links(graph))

    def test_comments_and_strings_ignored(self):
        for dialect in ("postgres", "mysql", "sqlite"):
            graph = sqlplugin.extract(load(dialect))
            with self.subTest(dialect=dialect):
                self.assertFalse([entity for entity in graph["entities"] if "ghost" in entity])
        text = "CREATE TABLE a (x text DEFAULT '--'); CREATE TABLE b (y int);"
        self.assertEqual(sorted(sqlplugin.extract({"m.sql": text})["entities"]),
                         ["db_table:a", "db_table:b"])

    def test_line_numbers(self):
        files = load("postgres")
        text = files["db/001_init.sql"]
        graph = sqlplugin.extract(files)
        self.assertEqual(graph["entities"]["db_table:users"]["events"][0],
                         ["db/001_init.sql", line_of(text, 'public."Users"'), "create"])
        self.assertEqual(graph["entities"]["db_routine:orders_touch"]["events"],
                         [["db/001_init.sql", line_of(text, "CREATE TRIGGER orders_touch"), "create"]])
        crlf = {name: body.replace("\n", "\r\n") for name, body in files.items()}
        self.assertEqual(sqlplugin.extract(crlf), graph, "CRLF files give the same graph and line numbers")

    def test_rename_and_drop_events(self):
        graph = sqlplugin.extract(load("postgres"))
        orders = [op for _file, _line, op in graph["entities"]["db_table:orders"]["events"]]
        self.assertEqual(orders, ["create", "alter", "rename-to:purchases"])
        self.assertEqual(graph["entities"]["db_table:purchases"]["events"],
                         [["db/002_change.sql", 3, "rename-from:orders"]])
        self.assertEqual(graph["entities"]["db_table:legacy_view"]["events"],
                         [["db/002_change.sql", 4, "drop"]])
        mysql = sqlplugin.extract(load("mysql"))
        self.assertEqual(mysql["entities"]["db_table:bills"]["events"][0][2], "rename-from:invoices")


class DbQueryTest(unittest.TestCase):
    def test_entity_query_identity(self):
        repo = make_git_repo(self)
        files = {f"db/migrations/{name.split('/')[-1]}": text for name, text in load("postgres").items()}
        files["app/repo.py"] = "rows = db.query('SELECT * FROM Orders')\n"
        files["README.md"] = "The orders table holds purchases.\n"
        head = git_commit(repo, files)
        result = run_impact(repo, "db_table:orders", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        identity = report["db_impact"]
        self.assertEqual(identity["status"], "available")
        self.assertEqual(identity["commit"], head)
        self.assertRegex(identity["graph"], r"\A[0-9a-f]{64}\Z")
        db_rows = [row for row in report["rows"] if row["layer"] == "db"]
        canonical = json.dumps(db_rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        self.assertEqual(identity["output"], hashlib.sha256(canonical.encode("utf-8")).hexdigest())
        found = {(row["source"], row["detail"]) for row in db_rows}
        self.assertIn(("db/migrations/001_init.sql", "create"), found)
        self.assertIn(("app/repo.py", "code-ref"), found)
        self.assertIn(("db/migrations/001_init.sql", "foreign-key from db_table:orders to db_table:users"), found)
        self.assertIn(("db/migrations/001_init.sql", "trigger-on by db_routine:orders_touch"), found)
        text = run_impact(repo, "db_table:orders")
        self.assertIn(f"db-impact: commit={head} graph={identity['graph']} output={identity['output']}",
                      text.stdout)

    def test_unknown_entity_is_usage_error(self):
        repo = make_git_repo(self)
        git_commit(repo, {"db/001.sql": "CREATE TABLE a (x int);\n"})
        result = run_impact(repo, "db_table:nope")
        self.assertEqual(result.returncode, 1)
        self.assertIn("OAH_IMPACT_UNKNOWN_ENTITY", result.stderr)

    def test_no_sql_unavailable(self):
        repo = make_git_repo(self)
        git_commit(repo, {"src/a.py": "a = 1\n"})
        result = run_impact(repo, "src/a.py")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("db-impact: unavailable (no SQL migrations found)", result.stdout.splitlines())
        entity = run_impact(repo, "db_table:a")
        self.assertEqual(entity.returncode, 1)
        self.assertIn("db-impact: unavailable (no SQL migrations found)", entity.stderr)


if __name__ == "__main__":
    unittest.main()
