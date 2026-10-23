# SQL 플러그인 검사 — Postgres·MySQL·SQLite 식 마이그레이션에서 테이블·뷰·루틴·트리거·인덱스·GRANT 와 연결을 뽑고, DB 질의가 그래프 신원을 함께 내는지 본다.
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import sys
import unittest

from oah.impact import sqlplugin
from tests.helpers import git_commit, make_git_repo
from tests.test_impact_map import SRC, run_impact

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

    def test_hostile_grants_finish_within_budget(self):
        code = ("from oah.impact import sqlplugin\n"
                "graph = sqlplugin.extract({'hostile.sql': 'GRANT x ' * 100000 + ';\\n' "
                "+ 'GRANT SELECT ON users TO app;\\n'})\n"
                "assert graph['links'] == [['db_grant:app', 'db_table:users', 'grant:SELECT', "
                "'hostile.sql', 2]], graph\n")
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8",
                                env=dict(os.environ, PYTHONPATH=SRC), timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_mysql_hash_comments_and_double_quoted_strings(self):
        text = ('#CREATE TABLE ghost_hash (id int);\n'
                'CREATE TABLE notes (body text DEFAULT "CREATE TABLE ghost_default (id int);");\n'
                'INSERT INTO notes VALUES ("CREATE TABLE ghost_string (id int);");\n'
                'INSERT INTO notes VALUES ("escaped \\" quote; CREATE TABLE ghost_escape (id int);");\n'
                'CREATE TABLE public."Real Table" (id int);\n')
        graph = sqlplugin.extract({"mysql.sql": text})
        self.assertEqual(set(graph["entities"]), {"db_table:notes", "db_table:real table"})
        self.assertEqual(graph["entities"]["db_table:real table"]["events"], [["mysql.sql", 5, "create"]])

    def test_grant_routine_signatures_split_at_top_level(self):
        for signature in ("integer, text", "numeric(10, 2), text", '"schema"."type", numeric(10, 2)'):
            with self.subTest(signature=signature):
                graph = sqlplugin.extract({"grant.sql":
                    f'GRANT EXECUTE ON FUNCTION public.f({signature}), "g"(integer) TO app;\n'
                    f'REVOKE EXECUTE ON FUNCTION public.f({signature}) FROM app;\n'})
                self.assertEqual(self.links(graph), {
                    ("db_grant:app", "db_routine:f", "grant:EXECUTE"),
                    ("db_grant:app", "db_routine:g", "grant:EXECUTE"),
                    ("db_grant:app", "db_routine:f", "revoke:EXECUTE"),
                })

    def test_quoted_identifier_lists_remain_identifiers(self):
        graph = sqlplugin.extract({"quoted.sql": 'DROP TABLE "first table", "second table";\n'})
        self.assertEqual(set(graph["entities"]), {"db_table:first table", "db_table:second table"})

    def test_long_quoted_identifier_lists_and_whitespace(self):
        names = [f'table {number}' for number in range(80)]
        for gap in (" ", " " * 20000):
            with self.subTest(gap=len(gap)):
                text = "DROP TABLE" + gap + ("," + gap).join(f'"{name}"' for name in names) + ";"
                graph = sqlplugin.extract({"quoted.sql": text})
                self.assertEqual(set(graph["entities"]), {"db_table:" + name for name in names})

    def test_postgres_hash_operators_preserve_view_sources(self):
        for expression in ("data #> '{key}'", "data #>> '{key}'", "bits # 7", "bits#7"):
            with self.subTest(expression=expression):
                graph = sqlplugin.extract({"operators.sql": "CREATE TABLE users (data jsonb, bits int);\n"
                    f"CREATE VIEW v AS SELECT {expression} AS foo FROM users;\n"})
                self.assertIn(("db_table:v", "db_table:users", "view-source"), self.links(graph))

    def test_grant_quoted_semicolons(self):
        for quote in ('"', '`', '['):
            close = ']' if quote == '[' else quote
            with self.subTest(quote=quote):
                graph = sqlplugin.extract({"grant.sql":
                    f'GRANT SELECT ON {quote}a; b{close} TO app;\n'})
                self.assertEqual(self.links(graph), {("db_grant:app", "db_table:a; b", "grant:SELECT")})

    def test_grant_long_whitespace_without_on_is_bounded(self):
        self.bounded_extract("GRANT app TO bob" + " " * 200000 + ";\n")

    def bounded_extract(self, hostile: str):
        # A child process enforces a generous deadline even if extraction never returns.
        code = ("import json, sys\nfrom oah.impact import sqlplugin\n"
                "graph = sqlplugin.extract({'hostile.sql': sys.stdin.read()})\n"
                "assert ['db_grant:app', 'db_table:users', 'grant:SELECT', 'hostile.sql', 2] "
                "in graph['links'], graph\n")
        result = subprocess.run([sys.executable, "-c", code], input=hostile +
                                'GRANT SELECT ON users TO app;\n', capture_output=True, text=True,
                                encoding="utf-8", env=dict(os.environ, PYTHONPATH=SRC), timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_tokenizer_hostile_inputs_are_bounded(self):
        for hostile in ('GRANT x ' * 100000 + ';\n',
                        'GRANT EXECUTE ON FUNCTION f(' + '(' * 50000 + ')' * 50000 + ') TO bob;\n',
                        'SELECT ' + '(' * 50000 + ')' * 50000 + ';\n'):
            with self.subTest(prefix=hostile[:30]):
                self.bounded_extract(hostile)
        code = ("import sys\nfrom oah.impact import sqlplugin\n"
                "assert sqlplugin.extract({'bad.sql': sys.stdin.read()})['entities'] == {}\n")
        for quote in ("'", '"', '`', '$tag$'):
            with self.subTest(quote=quote):
                result = subprocess.run([sys.executable, "-c", code],
                    input='SELECT ' + quote + 'x; CREATE TABLE ghost (id int); ' * 20000,
                    capture_output=True, text=True, encoding="utf-8",
                    env=dict(os.environ, PYTHONPATH=SRC), timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_tokenizer_quotes_comments_and_boundaries(self):
        text = ("# CREATE TABLE ghost_hash (id int);\n"
                "CREATE TABLE users (body text DEFAULT 'it''s; CREATE TABLE ghost_single (id int);');\n"
                'CREATE TABLE "a""; b" (id int);\n'
                'CREATE TABLE `c``; d` (id int);\n'
                "SELECT $tag$ CREATE TABLE ghost_dollar (id int); $tag$;\n"
                "/* outer; /* nested; */ CREATE TABLE ghost_comment (id int); */\n"
                "GRANT SELECT /* ; ON ghost TO bad */ ON users TO app;\n")
        graph = sqlplugin.extract({"quotes.sql": text})
        self.assertEqual(set(graph["entities"]),
                         {'db_table:users', 'db_table:a"; b', 'db_table:c`; d', 'db_grant:app'})
        self.assertEqual(graph["links"],
                         [["db_grant:app", "db_table:users", "grant:SELECT", "quotes.sql", 7]])

    def test_double_identifier_backslash_and_multiline_hash_comment(self):
        graph = sqlplugin.extract({"quotes.sql":
            'CREATE TABLE "trailing\\" (id int);\n'
            'SELECT 1\n# CREATE TABLE ghost (id int);\n;\n'})
        self.assertEqual(set(graph["entities"]), {'db_table:trailing\\'})

    def test_custom_delimiter_with_long_common_prefix_is_bounded(self):
        code = ("import sys\nfrom oah.impact import sqlplugin\n"
                "graph = sqlplugin.extract({'delimiter.sql': sys.stdin.read()})\n"
                "assert 'db_table:users' in graph['entities'], graph\n")
        delimiter = "a" * 30000 + "b"
        text = "DELIMITER " + delimiter + "\nSELECT " + "a" * 300000 + delimiter + \
               "\nCREATE TABLE users (id int)" + delimiter + "\n"
        result = subprocess.run([sys.executable, "-c", code], input=text, capture_output=True,
                                text=True, encoding="utf-8", env=dict(os.environ, PYTHONPATH=SRC), timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_transaction_boundaries_do_not_hide_entities(self):
        graph = sqlplugin.extract({"transaction.sql":
            'BEGIN TRANSACTION;\nCREATE TABLE "users" (id int);\n'
            'CREATE VIEW v AS SELECT * FROM users;\nCOMMIT;\n'})
        self.assertEqual(set(graph["entities"]), {"db_table:users", "db_table:v"})
        self.assertEqual(graph["entities"]["db_table:users"]["events"], [["transaction.sql", 2, "create"]])
        self.assertIn(("db_table:v", "db_table:users", "view-source"), self.links(graph))

    def test_grant_column_privileges_keep_original_spelling(self):
        graph = sqlplugin.extract({"grant.sql": 'GRANT SELECT ("odd name"), UPDATE(id) ON users TO app;'})
        self.assertEqual(self.links(graph), {
            ("db_grant:app", "db_table:users", 'grant:SELECT ("ODD NAME"),UPDATE(ID)')})

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
    def test_index_only_migration_returns_table_code_references(self):
        repo = make_git_repo(self)
        git_commit(repo, {"db/001.sql": "CREATE TABLE users (id int);\n",
                          "db/002.sql": "CREATE INDEX users_idx ON users(id);\n",
                          "src/store.py": "query('SELECT * FROM users')\n"})
        result = run_impact(repo, "db/002.sql", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = [row for row in json.loads(result.stdout)["rows"] if row["layer"] == "db"]
        self.assertIn(("src/store.py", "uses db_table:users"), {(r["source"], r["detail"]) for r in rows})
        self.assertIn(("db/002.sql", "index users_idx db_table:users"),
                      {(r["source"], r["detail"]) for r in rows})

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
