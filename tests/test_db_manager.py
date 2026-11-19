"""Unit tests for PostgreSQL connection, schema reset, and DDL application."""

from __future__ import annotations

import psycopg
from psycopg import sql
import pytest

from pgadmin_schemadiff.config import DatabaseConfig
from pgadmin_schemadiff.db_manager import (
    apply_ddl_scripts,
    connect_db,
    ensure_and_reset_schema,
    prepare_database,
)
from pgadmin_schemadiff.ddl_loader import DDLScript
from pgadmin_schemadiff.exceptions import DatabaseError


class FakeCursor:
    def __init__(self, responses: list[list[tuple]] | None = None, fail_on: str | None = None) -> None:
        self.executed: list[tuple[str, object]] = []
        self._responses = list(responses or [])
        self._last_rows: list[tuple] = []
        self._fail_on = fail_on

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, query: object, params: object = None) -> None:
        rendered = query.as_string(None) if isinstance(query, sql.Composable) else str(query)
        self.executed.append((rendered, params))
        if self._fail_on and self._fail_on in rendered:
            raise psycopg.OperationalError("simulated error")
        if rendered.lstrip().upper().startswith("SELECT"):
            self._last_rows = self._responses.pop(0) if self._responses else []

    def fetchone(self) -> tuple | None:
        return self._last_rows[0] if self._last_rows else None

    def fetchall(self) -> list[tuple]:
        return list(self._last_rows)


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor
        self.closed = False

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, *args: object) -> None:
        self.closed = True

    def cursor(self) -> FakeCursor:
        return self._cursor


def test_connect_db_success_and_error(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = DatabaseConfig(host="localhost", port=5432, database="db1", user="pg", password="pw")
    captured: dict[str, object] = {}

    def fake_connect(**kwargs: object) -> str:
        captured.update(kwargs)
        return "conn_obj"

    monkeypatch.setattr(psycopg, "connect", fake_connect)
    assert connect_db(cfg) == "conn_obj"
    assert captured["autocommit"] is True
    assert captured["dbname"] == "db1"

    def fail_connect(**kwargs: object) -> None:
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(psycopg, "connect", fail_connect)
    with pytest.raises(DatabaseError, match="Failed to connect"):
        connect_db(cfg)


def test_ensure_and_reset_schema_creates_missing_schema() -> None:
    cfg = DatabaseConfig(host="h", user="u", search_path="custom_sch, public")
    cur = FakeCursor(responses=[[]])  # SELECT 1 FROM pg_namespace -> not found
    conn = FakeConnection(cur)

    ensure_and_reset_schema(conn, cfg)  # type: ignore[arg-type]

    assert any('CREATE SCHEMA "custom_sch"' in stmt for stmt, _ in cur.executed)


def test_ensure_and_reset_schema_drops_all_object_kinds() -> None:
    cfg = DatabaseConfig(host="h", user="u", search_path="app")
    # Order of SELECTs:
    # 1. schema exists -> [(1,)]
    # 2-6. mview, view, foreign_table, table, sequence
    # 7. routines (function, procedure, aggregate)
    # 8-15. domain, type, ts_config, ts_dict, ts_template, ts_parser, collation, statistics
    # 16. operators
    # 17. extensions
    responses = [
        [(1,)],
        [("mv_1",)],
        [("v_1",)],
        [("ft_1",)],
        [("tbl_1",)],
        [("seq_1",)],
        [
            ("fn_1", "f", "integer, text"),
            ("proc_1", "p", "integer"),
            ("agg_1", "a", "integer"),
        ],
        [("dom_1",)],
        [("typ_1",)],
        [("tsc_1",)],
        [("tsd_1",)],
        [("tst_1",)],
        [("tsp_1",)],
        [("col_1",)],
        [("stx_1",)],
        [("+", "integer", "integer"), ("!", "NONE", "integer")],
        [("pg_trgm",)],
    ]
    cur = FakeCursor(responses=responses)
    conn = FakeConnection(cur)

    ensure_and_reset_schema(conn, cfg)  # type: ignore[arg-type]

    stmts = [stmt for stmt, _ in cur.executed]
    assert 'DROP MATERIALIZED VIEW IF EXISTS "app"."mv_1" CASCADE' in stmts
    assert 'DROP VIEW IF EXISTS "app"."v_1" CASCADE' in stmts
    assert 'DROP FOREIGN TABLE IF EXISTS "app"."ft_1" CASCADE' in stmts
    assert 'DROP TABLE IF EXISTS "app"."tbl_1" CASCADE' in stmts
    assert 'DROP SEQUENCE IF EXISTS "app"."seq_1" CASCADE' in stmts
    assert 'DROP FUNCTION IF EXISTS "app"."fn_1"(integer, text) CASCADE' in stmts
    assert 'DROP PROCEDURE IF EXISTS "app"."proc_1"(integer) CASCADE' in stmts
    assert 'DROP AGGREGATE IF EXISTS "app"."agg_1"(integer) CASCADE' in stmts
    assert 'DROP DOMAIN IF EXISTS "app"."dom_1" CASCADE' in stmts
    assert 'DROP TYPE IF EXISTS "app"."typ_1" CASCADE' in stmts
    assert 'DROP TEXT SEARCH CONFIGURATION IF EXISTS "app"."tsc_1" CASCADE' in stmts
    assert 'DROP TEXT SEARCH DICTIONARY IF EXISTS "app"."tsd_1" CASCADE' in stmts
    assert 'DROP TEXT SEARCH TEMPLATE IF EXISTS "app"."tst_1" CASCADE' in stmts
    assert 'DROP TEXT SEARCH PARSER IF EXISTS "app"."tsp_1" CASCADE' in stmts
    assert 'DROP COLLATION IF EXISTS "app"."col_1" CASCADE' in stmts
    assert 'DROP STATISTICS IF EXISTS "app"."stx_1" CASCADE' in stmts
    assert 'DROP OPERATOR IF EXISTS "app".+(integer, integer) CASCADE' in stmts
    assert 'DROP OPERATOR IF EXISTS "app".!(NONE, integer) CASCADE' in stmts
    assert 'DROP EXTENSION IF EXISTS "pg_trgm" CASCADE' in stmts


def test_ensure_and_reset_schema_rejects_bad_operator_and_wraps_db_error() -> None:
    cfg = DatabaseConfig(host="h", user="u", search_path="app")
    responses = [
        [(1,)],
        [], [], [], [], [],
        [],
        [], [], [], [], [], [], [], [],
        [("bad;op", "integer", "integer")],
    ]
    cur = FakeCursor(responses=responses)
    with pytest.raises(DatabaseError, match="Unexpected operator name"):
        ensure_and_reset_schema(FakeConnection(cur), cfg)  # type: ignore[arg-type]

    failing_cur = FakeCursor(responses=[[(1,)]], fail_on="pg_class")
    with pytest.raises(DatabaseError, match="Failed to reset schema"):
        ensure_and_reset_schema(FakeConnection(failing_cur), cfg)  # type: ignore[arg-type]


def test_apply_ddl_scripts_and_prepare_database(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = DatabaseConfig(host="h", user="u", search_path='"$user", app, public')
    cur = FakeCursor()
    conn = FakeConnection(cur)

    scripts = [
        DDLScript(name="00_empty.sql", sql="   \n  "),
        DDLScript(name="01_init.sql", sql="CREATE TABLE t (id int);"),
    ]
    apply_ddl_scripts(conn, cfg, scripts)  # type: ignore[arg-type]

    assert cur.executed == [
        ('SET search_path TO "$user", "app", "public"', None),
        ("CREATE TABLE t (id int);", None),
    ]

    failing_cur = FakeCursor(fail_on="BAD SQL")
    with pytest.raises(DatabaseError, match="02_bad.sql"):
        apply_ddl_scripts(
            FakeConnection(failing_cur),  # type: ignore[arg-type]
            cfg,
            [DDLScript(name="02_bad.sql", sql="BAD SQL;")],
        )

    full_cur = FakeCursor(responses=[[]])
    full_conn = FakeConnection(full_cur)
    monkeypatch.setattr("pgadmin_schemadiff.db_manager.connect_db", lambda _cfg: full_conn)
    prepare_database(cfg, [DDLScript(name="01.sql", sql="CREATE TABLE x (id int);")])
    assert full_conn.closed is True
    assert any("CREATE TABLE x" in s for s, _ in full_cur.executed)
