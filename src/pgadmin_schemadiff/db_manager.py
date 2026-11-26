"""PostgreSQL connection, schema reset, and DDL execution helpers."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
import re

import psycopg
from psycopg import sql

from pgadmin_schemadiff.config import DatabaseConfig
from pgadmin_schemadiff.ddl_loader import DDLScript, StreamedDDL
from pgadmin_schemadiff.exceptions import DatabaseError
from pgadmin_schemadiff.psql_runner import run_psql_files

_OPRNAME_RE = re.compile(r"^[+\-*/<>=~!@#%^&|`?]+$")


@dataclass(frozen=True)
class _SimpleObjectSpec:
    drop_prefix: str
    catalog_table: str
    alias: str
    name_col: str
    namespace_col: str
    extra_where: str = ""
    order_by: str = ""


_SIMPLE_OBJECT_SPECS: tuple[_SimpleObjectSpec, ...] = (
    _SimpleObjectSpec(
        drop_prefix="DROP MATERIALIZED VIEW IF EXISTS",
        catalog_table="pg_class",
        alias="c",
        name_col="relname",
        namespace_col="relnamespace",
        extra_where="AND c.relkind = 'm'",
    ),
    _SimpleObjectSpec(
        drop_prefix="DROP VIEW IF EXISTS",
        catalog_table="pg_class",
        alias="c",
        name_col="relname",
        namespace_col="relnamespace",
        extra_where="AND c.relkind = 'v'",
    ),
    _SimpleObjectSpec(
        drop_prefix="DROP FOREIGN TABLE IF EXISTS",
        catalog_table="pg_class",
        alias="c",
        name_col="relname",
        namespace_col="relnamespace",
        extra_where="AND c.relkind = 'f'",
    ),
    _SimpleObjectSpec(
        drop_prefix="DROP TABLE IF EXISTS",
        catalog_table="pg_class",
        alias="c",
        name_col="relname",
        namespace_col="relnamespace",
        extra_where="AND c.relkind IN ('r', 'p')",
        order_by="c.relispartition ASC, c.relname ASC",
    ),
    _SimpleObjectSpec(
        drop_prefix="DROP SEQUENCE IF EXISTS",
        catalog_table="pg_class",
        alias="c",
        name_col="relname",
        namespace_col="relnamespace",
        extra_where="AND c.relkind = 'S'",
    ),
)

_TRAILING_OBJECT_SPECS: tuple[_SimpleObjectSpec, ...] = (
    _SimpleObjectSpec(
        drop_prefix="DROP DOMAIN IF EXISTS",
        catalog_table="pg_type",
        alias="t",
        name_col="typname",
        namespace_col="typnamespace",
        extra_where="AND t.typtype = 'd'",
    ),
    _SimpleObjectSpec(
        drop_prefix="DROP TYPE IF EXISTS",
        catalog_table="pg_type",
        alias="t",
        name_col="typname",
        namespace_col="typnamespace",
        extra_where=(
            "AND (t.typtype IN ('e', 'r', 'm') OR ("
            "t.typtype = 'c' AND EXISTS ("
            "SELECT 1 FROM pg_catalog.pg_class c "
            "WHERE c.oid = t.typrelid AND c.relkind = 'c'"
            ")))"
        ),
    ),
    _SimpleObjectSpec(
        drop_prefix="DROP TEXT SEARCH CONFIGURATION IF EXISTS",
        catalog_table="pg_ts_config",
        alias="cfg",
        name_col="cfgname",
        namespace_col="cfgnamespace",
    ),
    _SimpleObjectSpec(
        drop_prefix="DROP TEXT SEARCH DICTIONARY IF EXISTS",
        catalog_table="pg_ts_dict",
        alias="dct",
        name_col="dictname",
        namespace_col="dictnamespace",
    ),
    _SimpleObjectSpec(
        drop_prefix="DROP TEXT SEARCH TEMPLATE IF EXISTS",
        catalog_table="pg_ts_template",
        alias="tmpl",
        name_col="tmplname",
        namespace_col="tmplnamespace",
    ),
    _SimpleObjectSpec(
        drop_prefix="DROP TEXT SEARCH PARSER IF EXISTS",
        catalog_table="pg_ts_parser",
        alias="prs",
        name_col="prsname",
        namespace_col="prsnamespace",
    ),
    _SimpleObjectSpec(
        drop_prefix="DROP COLLATION IF EXISTS",
        catalog_table="pg_collation",
        alias="col",
        name_col="collname",
        namespace_col="collnamespace",
    ),
    _SimpleObjectSpec(
        drop_prefix="DROP STATISTICS IF EXISTS",
        catalog_table="pg_statistic_ext",
        alias="stx",
        name_col="stxname",
        namespace_col="stxnamespace",
    ),
)


def connect_db(db_config: DatabaseConfig) -> psycopg.Connection:
    """Open a PostgreSQL connection with autocommit enabled."""
    try:
        return psycopg.connect(
            host=db_config.host,
            port=db_config.port,
            dbname=db_config.database,
            user=db_config.user,
            password=db_config.password,
            sslmode=db_config.sslmode,
            autocommit=True,
        )
    except psycopg.Error as exc:
        raise DatabaseError(
            f"Failed to connect to PostgreSQL ({db_config.host}:{db_config.port}/"
            f"{db_config.database} as {db_config.user}): {exc}"
        ) from exc


def _drop_simple_objects(
    cur: psycopg.Cursor,
    schema: str,
    spec: _SimpleObjectSpec,
) -> None:
    order_clause = spec.order_by or f"{spec.alias}.{spec.name_col} ASC"
    query = (
        f"SELECT {spec.alias}.{spec.name_col} "
        f"FROM pg_catalog.{spec.catalog_table} {spec.alias} "
        f"JOIN pg_catalog.pg_namespace n ON n.oid = {spec.alias}.{spec.namespace_col} "
        f"WHERE n.nspname = %s "
        f"{spec.extra_where} "
        f"AND NOT EXISTS ("
        f"SELECT 1 FROM pg_catalog.pg_depend d "
        f"WHERE d.classid = 'pg_catalog.{spec.catalog_table}'::regclass "
        f"AND d.objid = {spec.alias}.oid AND d.deptype = 'e'"
        f") "
        f"ORDER BY {order_clause}"
    )
    cur.execute(query, (schema,))
    for (obj_name,) in cur.fetchall():
        stmt = sql.SQL("{} {}.{} CASCADE").format(
            sql.SQL(spec.drop_prefix),
            sql.Identifier(schema),
            sql.Identifier(obj_name),
        )
        cur.execute(stmt)


def _drop_routines(cur: psycopg.Cursor, schema: str) -> None:
    query = """
        SELECT p.proname,
               p.prokind::text,
               pg_catalog.pg_get_function_identity_arguments(p.oid)
        FROM pg_catalog.pg_proc p
        JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = %s
          AND p.prokind IN ('f', 'p', 'a', 'w')
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.pg_depend d
              WHERE d.classid = 'pg_catalog.pg_proc'::regclass
                AND d.objid = p.oid
                AND d.deptype = 'e'
          )
        ORDER BY p.proname, p.oid
    """
    cur.execute(query, (schema,))
    for proname, prokind, identity_args in cur.fetchall():
        if prokind == "p":
            drop_kw = "DROP PROCEDURE IF EXISTS"
        elif prokind == "a":
            drop_kw = "DROP AGGREGATE IF EXISTS"
        else:
            drop_kw = "DROP FUNCTION IF EXISTS"

        stmt = sql.SQL("{} {}.{}({}) CASCADE").format(
            sql.SQL(drop_kw),
            sql.Identifier(schema),
            sql.Identifier(proname),
            sql.SQL(identity_args or ""),
        )
        cur.execute(stmt)


def _drop_operators(cur: psycopg.Cursor, schema: str) -> None:
    query = """
        SELECT op.oprname,
               CASE WHEN op.oprleft = 0 THEN 'NONE' ELSE op.oprleft::regtype::text END,
               CASE WHEN op.oprright = 0 THEN 'NONE' ELSE op.oprright::regtype::text END
        FROM pg_catalog.pg_operator op
        JOIN pg_catalog.pg_namespace n ON n.oid = op.oprnamespace
        WHERE n.nspname = %s
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.pg_depend d
              WHERE d.classid = 'pg_catalog.pg_operator'::regclass
                AND d.objid = op.oid
                AND d.deptype = 'e'
          )
        ORDER BY op.oprname, op.oid
    """
    cur.execute(query, (schema,))
    for oprname, left_type, right_type in cur.fetchall():
        if not _OPRNAME_RE.match(oprname):
            raise DatabaseError(f"Unexpected operator name '{oprname}' in schema '{schema}'.")
        stmt = sql.SQL("DROP OPERATOR IF EXISTS {}.{}({}, {}) CASCADE").format(
            sql.Identifier(schema),
            sql.SQL(oprname),
            sql.SQL(left_type),
            sql.SQL(right_type),
        )
        cur.execute(stmt)


def _drop_extensions(cur: psycopg.Cursor, schema: str) -> None:
    query = """
        SELECT e.extname
        FROM pg_catalog.pg_extension e
        JOIN pg_catalog.pg_namespace n ON n.oid = e.extnamespace
        WHERE n.nspname = %s
          AND e.extname <> 'plpgsql'
          AND (
              e.extowner = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = current_user)
              OR EXISTS (
                  SELECT 1 FROM pg_catalog.pg_roles r
                  WHERE r.rolname = current_user AND r.rolsuper
              )
          )
        ORDER BY e.extname
    """
    cur.execute(query, (schema,))
    for (extname,) in cur.fetchall():
        stmt = sql.SQL("DROP EXTENSION IF EXISTS {} CASCADE").format(sql.Identifier(extname))
        cur.execute(stmt)


def ensure_and_reset_schema(
    conn: psycopg.Connection,
    db_config: DatabaseConfig,
) -> None:
    """Create the target schema if missing, or drop all objects inside it."""
    schema = db_config.primary_schema
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname = %s",
                (schema,),
            )
            exists = cur.fetchone() is not None
            if not exists:
                cur.execute(
                    sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
                )
                return

            for spec in _SIMPLE_OBJECT_SPECS:
                _drop_simple_objects(cur, schema, spec)

            _drop_routines(cur, schema)

            for spec in _TRAILING_OBJECT_SPECS:
                _drop_simple_objects(cur, schema, spec)

            _drop_operators(cur, schema)
            _drop_extensions(cur, schema)
    except DatabaseError:
        raise
    except psycopg.Error as exc:
        raise DatabaseError(
            f"Failed to reset schema '{schema}' on database '{db_config.database}': {exc}"
        ) from exc


def _build_set_search_path(db_config: DatabaseConfig) -> sql.Composed:
    parts = [
        sql.SQL('"$user"') if entry == "$user" else sql.Identifier(entry)
        for entry in db_config.search_path_schemas()
    ]
    return sql.SQL("SET search_path TO {}").format(sql.SQL(", ").join(parts))


def apply_ddl_scripts(
    conn: psycopg.Connection,
    db_config: DatabaseConfig,
    scripts: Iterable[DDLScript],
) -> None:
    """Execute DDL scripts in order after setting the configured ``search_path``."""
    set_search_path_stmt = _build_set_search_path(db_config)
    for script in scripts:
        if not script.sql.strip():
            continue
        try:
            with conn.cursor() as cur:
                cur.execute(set_search_path_stmt)
                cur.execute(script.sql)
        except psycopg.Error as exc:
            raise DatabaseError(
                f"Failed to execute DDL '{script.name}': {exc}"
            ) from exc


def apply_sql_script(
    conn: psycopg.Connection,
    db_config: DatabaseConfig,
    script: str,
    *,
    script_name: str = "diff script",
) -> None:
    """Execute a SQL script after setting the configured ``search_path``."""
    if not script.strip():
        return
    set_search_path_stmt = _build_set_search_path(db_config)
    try:
        with conn.cursor() as cur:
            cur.execute(set_search_path_stmt)
            cur.execute(script)
    except psycopg.Error as exc:
        tx_status = getattr(getattr(conn, "info", None), "transaction_status", None)
        if tx_status == psycopg.pq.TransactionStatus.INERROR:
            try:
                with conn.cursor() as cur:
                    cur.execute("ROLLBACK")
            except psycopg.Error:
                pass
        raise DatabaseError(
            f"Failed to apply {script_name} to database '{db_config.database}' "
            f"(schema '{db_config.primary_schema}'): {exc}"
        ) from exc


def apply_diff_script(
    db_config: DatabaseConfig,
    script: str,
) -> None:
    """Connect to the target PostgreSQL database and apply the generated diff script."""
    with connect_db(db_config) as conn:
        apply_sql_script(conn, db_config, script, script_name="diff script")


def prepare_database(
    db_config: DatabaseConfig,
    scripts: Iterable[DDLScript],
) -> None:
    """Reset the target schema in PostgreSQL and apply the given DDL scripts."""
    with connect_db(db_config) as conn:
        ensure_and_reset_schema(conn, db_config)
        apply_ddl_scripts(conn, db_config, scripts)


def prepare_database_files(
    db_config: DatabaseConfig,
    scripts: list[StreamedDDL],
    *,
    psql: Path | None,
) -> None:
    """Reset the target schema, then restore all SQL files in one transaction."""
    if scripts and psql is None:
        raise DatabaseError("psql is required to restore SQL files.")

    with connect_db(db_config) as conn:
        ensure_and_reset_schema(conn, db_config)
        set_search_path = _build_set_search_path(db_config).as_string(conn)

    if psql is not None:
        run_psql_files(psql, db_config, set_search_path, scripts)
