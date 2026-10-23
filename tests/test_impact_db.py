# SQL 플러그인 검사 — Postgres·MySQL·SQLite 식 마이그레이션에서 테이블·뷰·루틴·트리거·인덱스·GRANT 와 연결을 뽑고, DB 질의가 그래프 신원을 함께 내는지 본다.
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import textwrap
import unittest
from contextlib import closing
from itertools import product

from oah.impact import mapstore, sqlplugin
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
        graph = sqlplugin._extract(load("mysql"), "mysql")
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
            graph = sqlplugin._extract(load(dialect), dialect)
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
        graph = sqlplugin._extract({"mysql.sql": text}, "mysql")
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
                "graph = sqlplugin._extract({'delimiter.sql': sys.stdin.read()}, 'mysql')\n"
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

    def test_huge_first_word_and_separators_are_bounded(self):
        self.bounded_extract('x' * 256000 + ' ' + ',' * 256000 + ';\n')

    def test_quoted_routine_bodies_preserve_locations(self):
        for file, prefix in product(('postgres.sql', 'db/001_init.sql'), ('', 'E')):
            with self.subTest(file=file, prefix=prefix):
                graph = sqlplugin.extract({file:
                    "CREATE TABLE users(id int);\n"
                    "CREATE FUNCTION f() RETURNS int LANGUAGE sql AS " + prefix +
                    "'SELECT\n id FROM users';\n"
                    "SELECT 'FROM ghost';\n"})
                self.assertIn(['db_routine:f', 'db_table:users', 'uses', file, 3], graph['links'])
                self.assertNotIn('db_table:ghost', graph['entities'])

    def test_inline_mysql_hash_comments_do_not_hide_entities(self):
        for comment in ("a ' quote; CREATE TABLE ghost(id int);", 'a ; semicolon'):
            with self.subTest(comment=comment):
                graph = sqlplugin._extract({'db/001_init.sql':
                    'SELECT 1 # ' + comment + '\n;\nCREATE TABLE users(id int);'}, "mysql")
                self.assertEqual(set(graph['entities']), {'db_table:users'})
                self.assertEqual(graph['entities']['db_table:users']['events'], [['db/001_init.sql', 3, 'create']])

    def test_routine_expression_literals_stay_opaque(self):
        graph = sqlplugin.extract({'postgres.sql':
            'CREATE TABLE users(id int); CREATE TABLE ghost(id int);\n'
            'CREATE FUNCTION f() RETURNS int AS $body$\n'
            "BEGIN SELECT 'FROM ghost', $$ SELECT id FROM ghost $$; SELECT id FROM users; END;\n"
            '$body$ LANGUAGE sql;'})
        self.assertEqual(self.links(graph), {('db_routine:f', 'db_table:users', 'uses')})

    def test_escaped_routine_body_uses_physical_lines(self):
        graph = sqlplugin.extract({'postgres.sql':
            'CREATE TABLE users(id int);\n'
            "CREATE FUNCTION f() RETURNS int AS E'SELECT\\n id FROM users' LANGUAGE sql;"})
        self.assertIn(['db_routine:f', 'db_table:users', 'uses', 'postgres.sql', 2], graph['links'])

    def test_mysql_version_comment_view_is_reported(self):
        graph = sqlplugin._extract({'db/001_init.sql':
            '\n/*!50001 CREATE VIEW v AS SELECT * FROM users */;'}, "mysql")
        self.assertTrue(any(region['file'] == 'db/001_init.sql' and region['line'] == 2
                            and 'executable comment' in region['reason']
                            for region in graph['unparsed']))

    def test_mysql_version_comment_trigger_is_reported(self):
        graph = sqlplugin._extract({'db/001_init.sql':
            '\n/*!50003 CREATE TRIGGER t AFTER INSERT ON users FOR EACH ROW\n'
            'BEGIN UPDATE users SET id = 1; END */;'}, "mysql")
        self.assertTrue(any(region['file'] == 'db/001_init.sql' and region['line'] == 2
                            and 'executable comment' in region['reason']
                            for region in graph['unparsed']))

    def test_postgres_standard_routine_backslashes_do_not_create_ghosts(self):
        for file, backslash in product(('postgres.sql', 'db/001_init.sql'), ('\\', '\\\\')):
            with self.subTest(file=file, backslash=backslash):
                body = "SELECT concat(''" + backslash + "'', ''CREATE TABLE ghost(id int);'', ''" + backslash + "'')"
                graph = sqlplugin.extract({file:
                    "CREATE FUNCTION f() RETURNS text LANGUAGE sql AS '" + body + "';\n"
                    'CREATE TABLE following(id int);'})
                self.assertEqual(set(graph['entities']), {'db_routine:f', 'db_table:following'})
                self.assertEqual(graph['links'], [])
                self.assertEqual(graph['entities']['db_table:following']['events'],
                                 [[file, 2, 'create']])

    def test_standard_strings_and_hash_operators_ignore_file_names(self):
        for file in ('db/001_init.sql', 'mysql.sql', 'postgres.sql', 'sqlite.sql'):
            with self.subTest(file=file):
                graph = sqlplugin.extract({file:
                    "SELECT '\\';\nCREATE TABLE users(id int);\n"
                    "CREATE VIEW v AS SELECT bits # mask FROM users;\n"
                    "CREATE TABLE following(id int);"})
                self.assertEqual(set(graph['entities']),
                                 {'db_table:users', 'db_table:v', 'db_table:following'})
                self.assertIn(('db_table:v', 'db_table:users', 'view-source'), self.links(graph))

    def test_engine_inside_standard_literal_keeps_following_table(self):
        text = ("CREATE TABLE t(a text DEFAULT '\\', b text DEFAULT ') ENGINE=InnoDB; SELECT ');\n"
                "SELECT '\\';\nCREATE TABLE following(id int);")
        for file in ('postgres.sql', 'db/001_init.sql'):
            with self.subTest(file=file):
                graph = sqlplugin.extract({file: text})
                self.assertEqual(set(graph['entities']), {'db_table:t', 'db_table:following'})
                self.assertEqual(graph['entities']['db_table:following']['events'], [[file, 3, 'create']])

    def test_delimiter_column_keeps_standard_semantics(self):
        text = ("CREATE TABLE t (\n delimiter text,\n a text DEFAULT '\\'\n);\n"
                'CREATE TABLE following(id int);')
        with closing(sqlite3.connect(':memory:')) as connection:
            connection.executescript(text)
            self.assertEqual(connection.execute('PRAGMA table_info(t)').fetchall()[0][1], 'delimiter')
        for file in ('postgres.sql', 'db/001_init.sql'):
            with self.subTest(file=file):
                graph = sqlplugin.extract({file: text})
                self.assertEqual(set(graph['entities']), {'db_table:t', 'db_table:following'})
                self.assertFalse(any('directive' in row['reason'] for row in graph['unparsed']))
                self.assertEqual(graph['entities']['db_table:following']['events'], [[file, 5, 'create']])

    def test_content_markers_never_select_mysql(self):
        for marker in ('DELIMITER ;\n', 'CREATE TABLE marker(id int) ENGINE=InnoDB;\n'):
            with self.subTest(marker=marker):
                text = marker + "SELECT '\\';\nCREATE TABLE following(id int);\n/*! SELECT 1 */;"
                graph = sqlplugin.extract({'db/001_init.sql': text})
                expected = {'db_table:following'}
                if marker.startswith('CREATE'):
                    expected.add('db_table:marker')
                self.assertEqual(set(graph['entities']), expected)
                self.assertFalse(any('executable comment' in row['reason'] for row in graph['unparsed']))
                self.assertFalse(any(token.kind == 'directive' for token in sqlplugin._tokenize(text)))

    def test_configured_mysql_markers_under_neutral_paths(self):
        for marker in ('DELIMITER ;\n', 'CREATE TABLE marker(id int) ENGINE=InnoDB;\n'):
            with self.subTest(marker=marker):
                graph = sqlplugin._extract({'db/001_init.sql': marker +
                    "SELECT 'escaped \\' quote; CREATE TABLE ghost(id int);';\n"
                    "SELECT 1 # ' quote; CREATE TABLE ghost_hash(id int);\n;\n"
                    "/*!50001 CREATE VIEW hidden AS SELECT 1 */;\n"
                    "CREATE TABLE following(id int);"}, 'mysql')
                expected = {'db_table:following'}
                if marker.startswith('CREATE'):
                    expected.add('db_table:marker')
                self.assertEqual(set(graph['entities']), expected)
                self.assertTrue(any('executable comment' in row['reason'] for row in graph['unparsed']))

    def test_marker_text_in_literals_and_comments_keeps_standard_rules(self):
        for marker in ("-- DELIMITER //\n", "/* CREATE TABLE m(id int) ENGINE=InnoDB; */\n",
                       "SELECT 'DELIMITER //';\n",
                       "SELECT 'CREATE TABLE m(id int) ENGINE=InnoDB;';\n",
                       'CREATE TABLE m(engine text DEFAULT \'ENGINE=InnoDB\');\n'):
            with self.subTest(marker=marker):
                graph = sqlplugin.extract({'db/001_init.sql': marker +
                    "SELECT '\\';\nCREATE TABLE following(id int);\n"
                    "/*!50001 CREATE VIEW hidden AS SELECT 1 */;"})
                self.assertIn('db_table:following', graph['entities'])
                self.assertFalse(any('executable comment' in row['reason'] for row in graph['unparsed']))

    def test_backslash_before_quoted_markers_keeps_standard_rules(self):
        for marker in ('DELIMITER ;', 'CREATE TABLE hidden(id int) ENGINE=InnoDB;'):
            with self.subTest(marker=marker):
                graph = sqlplugin.extract({'db/001_init.sql':
                    "SELECT '\\';\nSELECT '\n" + marker + "\n';\nSELECT '\\';\n"
                    'CREATE TABLE following(id int);'})
                self.assertEqual(set(graph['entities']), {'db_table:following'})

    def test_separated_E_prefix_does_not_decode_routine_backslashes(self):
        body = "SELECT concat(''\\'', ''CREATE TABLE ghost(id int);'', ''\\'')"
        for prefix in ('E ', 'E/* gap */'):
            with self.subTest(prefix=prefix):
                graph = sqlplugin.extract({'db/001_init.sql':
                    "CREATE FUNCTION f() RETURNS text AS " + prefix + "'" + body + "';\n"
                    'CREATE TABLE following(id int);'})
                self.assertEqual(set(graph['entities']), {'db_routine:f', 'db_table:following'})

    def test_standard_marker_text_is_bounded(self):
        code = textwrap.dedent('''
            import sys
            from oah.impact import sqlplugin
            graph = sqlplugin.extract({'db/001_init.sql': sys.stdin.read()})
            assert set(graph['entities']) == {'db_table:wide', 'db_table:following'}, graph['entities']
            assert not any('executable comment' in row['reason'] for row in graph['unparsed'])
        ''')
        text = ('/*' + 'ENGINE=InnoDB DELIMITER // ' * 20000 + '*/\n'
                'CREATE TABLE wide(' + 'engine int, ' * 40000 + 'id int) ENGINE=InnoDB;\n'
                "SELECT '\\';\n"
                '/*!50001 CREATE VIEW hidden AS SELECT 1 */;\nCREATE TABLE following(id int);')
        result = subprocess.run([sys.executable, '-c', code], input=text, capture_output=True,
                                text=True, encoding='utf-8', env=dict(os.environ, PYTHONPATH=SRC), timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_executable_comments_are_bounded_and_reported(self):
        code = textwrap.dedent('''
            import sys
            from oah.impact import sqlplugin
            graph = sqlplugin._extract({'db/001_init.sql': sys.stdin.read()}, 'mysql')
            regions = [region for region in graph['unparsed']
                       if 'executable comment' in region['reason']]
            assert len(regions) == 20001, len(regions)
            assert regions[0]['line'] == 2, regions[0]
            assert set(graph['entities']) == {'db_table:following'}, graph['entities']
        ''')
        text = ('\n/*!' + '5' * 256000 + ' CREATE TABLE hidden(id int); */;\n' +
                '/*! CREATE VIEW hidden AS SELECT 1 */;\n' * 20000 +
                'CREATE TABLE following(id int);')
        result = subprocess.run([sys.executable, '-c', code], input=text, capture_output=True,
                                text=True, encoding='utf-8', env=dict(os.environ, PYTHONPATH=SRC), timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_compound_body_extracts_nested_ddl(self):
        text = ('DELIMITER //\nCREATE PROCEDURE p()\nBEGIN\n'
                '  CREATE TABLE scratch(id int);\n'
                '  CREATE INDEX scratch_idx ON scratch(id);\n'
                'END//\nDELIMITER ;\nCREATE TABLE following(id int);\n')
        graph = sqlplugin._extract({'mysql.sql': text}, 'mysql')
        self.assertEqual(set(graph['entities']), {'db_routine:p', 'db_table:scratch', 'db_table:following'})
        self.assertEqual(graph['entities']['db_table:scratch']['events'], [['mysql.sql', 4, 'create']])
        self.assertEqual(graph['entities']['db_table:scratch']['indexes'], [['scratch_idx', 'mysql.sql', 5]])

    def test_unparsed_sql_regions_are_explicit(self):
        for sql in ("DO 'SELECT 1';", 'CREATE UNRECOGNISED widget;', "SELECT 'unterminated",
                    '/* unterminated', 'DELIMITER //\nSELECT 1//',
                    "CREATE FUNCTION f() RETURNS int AS 'SELECT 1' LANGUAGE sql;",
                    'CREATE PROCEDURE p() BEGIN SELECT 1; END;'):
            with self.subTest(sql=sql):
                graph = sqlplugin.extract({'partial.sql': '\n' + sql})
                self.assertTrue(graph['unparsed'])
                self.assertTrue(all(region['file'] == 'partial.sql' and region['line'] >= 2
                                    and region['reason'] for region in graph['unparsed']))

    def test_construct_generators_scale_below_quadratic(self):
        # Calibrated batches and minimum timings reduce timer/scheduler/GC noise.
        # At 8x input, a 24x limit separates linear (~8x) from quadratic (~64x) growth.
        code = textwrap.dedent('''
            import gc, time, unittest
            from oah.impact import sqlplugin
            generators = {
                'identifiers': lambda n: 'x' * n + ' ' + ',' * n + ';',
                'separators': lambda n: 'SELECT ' + ',' * n + ';',
                'quotes': lambda n: "SELECT '" + "x''" * n + "';",
                'comments': lambda n: '/*' * n + '*/' * n + 'SELECT 1;',
                'dollar tags': lambda n: '$' + 'a' * n + '$SELECT 1$' + 'a' * n + '$;',
                'parentheses': lambda n: 'SELECT ' + '(' * n + ')' * n + ';',
                'GRANT without ON': lambda n: 'GRANT app TO bob' + ' ' * n + ';',
                'nested routines': lambda n: 'CREATE PROCEDURE p() BEGIN ' * n +
                                             'SELECT 1;' + 'END;' * n,
            }
            def check_scaling(name, generate, extract=sqlplugin.extract, clock=time.perf_counter):
                inputs = [generate(size) for size in (2000, 16000)]
                def batch(text, repetitions):
                    gc.collect()
                    start = clock()
                    for _ in range(repetitions):
                        extract({'stress.sql': text})
                    elapsed = clock() - start
                    assert elapsed < 12, (name, len(text), elapsed)
                    return elapsed

                # Ten milliseconds dwarfs timer resolution; three minima and the wide
                # ratio margin tolerate CI noise without making eight generators slow.
                repetitions = 1
                elapsed = batch(inputs[0], repetitions)
                while elapsed < 0.01:
                    repetitions *= 2
                    elapsed = batch(inputs[0], repetitions)

                times = []
                for text in inputs:
                    times.append(min(batch(text, repetitions) for _ in range(3)))
                assert times[1] / times[0] < 24, (name + ': scaling ratio', times)
                print(name, round(times[1] / times[0], 3), times)

            for name, generate in generators.items():
                check_scaling(name, generate)

            # Real quadratic work must be rejected even when single runs are below 50 ms.
            def quadratic_extract(files):
                text = files['stress.sql']
                for _ in range(len(text) // 100):
                    text.count('x')

            for size in (2000, 16000):
                text = 'x' * size
                samples = []
                for _ in range(3):
                    start = time.perf_counter()
                    quadratic_extract({'stress.sql': text})
                    samples.append(time.perf_counter() - start)
                assert min(samples) < 0.05, (size, samples)
            with unittest.TestCase().assertRaisesRegex(AssertionError, 'quadratic real: scaling ratio'):
                check_scaling('quadratic real', lambda n: 'x' * n, quadratic_extract)
        ''')
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True,
                                encoding='utf-8', env=dict(os.environ, PYTHONPATH=SRC), timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_review_regression_fixture_corpus(self):
        # JSON-in-SQL comments keeps independent snippets and explicit expectations alongside
        # the dialect fixtures without executing one example in another example's context.
        cases = [json.loads(line.removeprefix('-- OAH-REGRESSION '))
                 for path in sorted(FIXTURES.glob('*/*.sql'))
                 for line in path.read_text(encoding='utf-8').splitlines()
                 if line.startswith('-- OAH-REGRESSION ')]
        self.assertEqual({case['round'] for case in cases}, {'R1', 'R2', 'R3', 'R4', 'R5', 'R6'})
        code = textwrap.dedent('''
            import json, sys
            from oah.impact import sqlplugin
            for case in json.load(sys.stdin):
                text = ''.join(part if isinstance(part, str) else part[0] * part[1]
                               for part in case['parts'])
                graph = (sqlplugin._extract({case['file']: text}, case['dialect']) if 'dialect' in case
                         else sqlplugin.extract({case['file']: text}))
                assert sorted(graph['entities']) == sorted(case['entities']), (case['id'], graph)
                links = sorted(link[:3] for link in graph['links'])
                assert links == sorted(case['links']), (case['id'], graph)
                for entity, indexes in case.get('indexes', {}).items():
                    assert graph['entities'][entity]['indexes'] == indexes, (case['id'], graph)
                for entity, events in case.get('events', {}).items():
                    assert graph['entities'][entity]['events'] == events, (case['id'], graph)
                for line in case.get('executable_lines', []):
                    assert any(region['file'] == case['file'] and region['line'] == line
                               and 'executable comment' in region['reason']
                               for region in graph['unparsed']), (case['id'], graph)
                if case.get('no_executable'):
                    assert not any('executable comment' in region['reason']
                                   for region in graph['unparsed']), (case['id'], graph)
        ''')
        result = subprocess.run([sys.executable, '-c', code], input=json.dumps(cases), capture_output=True,
                                text=True, encoding='utf-8', env=dict(os.environ, PYTHONPATH=SRC), timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rename_and_drop_events(self):
        graph = sqlplugin.extract(load("postgres"))
        orders = [op for _file, _line, op in graph["entities"]["db_table:orders"]["events"]]
        self.assertEqual(orders, ["create", "alter", "rename-to:purchases"])
        self.assertEqual(graph["entities"]["db_table:purchases"]["events"],
                         [["db/002_change.sql", 3, "rename-from:orders"]])
        self.assertEqual(graph["entities"]["db_table:legacy_view"]["events"],
                         [["db/002_change.sql", 4, "drop"]])
        mysql = sqlplugin._extract(load("mysql"), "mysql")
        self.assertEqual(mysql["entities"]["db_table:bills"]["events"][0][2], "rename-from:invoices")


class DbQueryTest(unittest.TestCase):
    def test_default_branch_only_db_identity(self):
        from tests.test_impact_default_branch import RemoteFixture

        fx = RemoteFixture(self)
        commit = fx.advance({"db/orders.sql": "CREATE TABLE orders (id int);\n",
                             "src/orders.py": "SELECT id FROM orders\n"})
        result = run_impact(fx.lane, "db_table:orders", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        identity = report["db_impact"]
        self.assertEqual(identity["status"], "available")
        graph = mapstore.snapshot(fx.pusher, commit)[1]["db"]
        self.assertEqual(identity["maps"], {"default_branch": {"commit": commit, "graph": mapstore.digest(graph)}})
        db_rows = [row for row in report["rows"] if row["layer"] == "db"]
        self.assertTrue(db_rows)
        self.assertEqual({row["found_in"] for row in db_rows}, {"default-branch"})
        self.assertEqual(identity["merged_output"], mapstore.digest(db_rows))
        self.assertNotIn("commit", identity, "a merged identity must not attribute rows to one commit")
        text = run_impact(fx.lane, "db_table:orders")
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn(f"default-branch=(commit={commit} graph={mapstore.digest(graph)})", text.stdout)
        self.assertIn(f"merged-output={identity['merged_output']}", text.stdout)

    def test_merged_db_identity_names_both_maps(self):
        from tests.test_impact_default_branch import RemoteFixture

        fx = RemoteFixture(self)
        migration = {"db/users.sql": "CREATE TABLE users (id int);\n"}
        mine = git_commit(fx.lane, {**migration, "src/lane_users.py": "SELECT id FROM users\n"})
        theirs = fx.advance({**migration, "db/audit.sql": "CREATE TABLE audit (id int);\n",
                             "src/default_users.py": "SELECT id FROM users\n"})
        result = run_impact(fx.lane, "db_table:users", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        identity = report["db_impact"]
        expected = {name: {"commit": commit, "graph": mapstore.digest(mapstore.snapshot(repo, commit)[1]["db"])}
                    for name, repo, commit in (("worktree", fx.lane, mine), ("default_branch", fx.pusher, theirs))}
        self.assertEqual(identity["status"], "available")
        self.assertEqual(identity["maps"], expected)
        self.assertNotEqual(expected["worktree"]["graph"], expected["default_branch"]["graph"])
        db_rows = [row for row in report["rows"] if row["layer"] == "db"]
        self.assertEqual({row["found_in"] for row in db_rows}, {"worktree", "default-branch", "both"})
        self.assertEqual(identity["merged_output"], mapstore.digest(db_rows))
        self.assertNotIn("output", identity, "the merged digest has a distinct name")
        self.assertNotIn("commit", identity, "each map carries its own commit")
        text = run_impact(fx.lane, "db_table:users")
        self.assertEqual(text.returncode, 0, text.stderr)
        for name, values in expected.items():
            self.assertIn(f"{name.replace('_', '-')}=(commit={values['commit']} graph={values['graph']})", text.stdout)
        self.assertIn(f"merged-output={identity['merged_output']}", text.stdout)

    def test_unparsed_sql_reports_survive_all_output_paths(self):
        repo = make_git_repo(self)
        git_commit(repo, {'db/001.sql': '\nCREATE UNRECOGNISED widget;\n', 'app.py': 'x = 1\n'})
        for args in (('app.py',), ('app.py', '--ref', 'HEAD'), ('build',), ('refresh',), ('status',)):
            with self.subTest(args=args):
                text = run_impact(repo, *args)
                self.assertEqual(text.returncode, 0, text.stderr)
                self.assertIn('OAH_IMPACT_SQL_UNPARSED', text.stderr)
                self.assertIn('db/001.sql:2', text.stderr)
                self.assertIn('review', text.stderr)
                self.assertIn('sql_dialect = "mysql"', text.stderr)
                result = run_impact(repo, *args, '--json')
                self.assertEqual(result.returncode, 0, result.stderr)
                report = json.loads(result.stdout)
                self.assertTrue(any('OAH_IMPACT_SQL_UNPARSED' in warning for warning in report['warnings']))
                self.assertTrue(any('sql_dialect = "mysql"' in warning for warning in report['warnings']))
                info = report.get('map', report)
                self.assertEqual(info['sql_unparsed'][0]['file'], 'db/001.sql')
                self.assertEqual(info['sql_unparsed'][0]['line'], 2)

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
