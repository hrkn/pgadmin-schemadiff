"""Unit tests for pglast-powered SQL tweaks and CLI tweak overrides."""

from __future__ import annotations

import pytest

from pgadmin_schemadiff.config import TweakOptions
from pgadmin_schemadiff.exceptions import ConfigError, SqlTweakError
from pgadmin_schemadiff.sql_tweaks import (
    apply_tweak_overrides,
    comment_meta_restrict_in_sql,
    omit_pg_attver_from_sql,
)


def test_apply_tweak_overrides_variants_and_errors() -> None:
    base = TweakOptions()
    assert apply_tweak_overrides(base, ["omit_pg_attver"]) == TweakOptions(
        omit_pg_attver=True, comment_meta_restrict=False
    )
    assert apply_tweak_overrides(
        base, ["omit_pg_attver,comment_meta_restrict"]
    ) == TweakOptions(omit_pg_attver=True, comment_meta_restrict=True)
    assert apply_tweak_overrides(
        TweakOptions(omit_pg_attver=True, comment_meta_restrict=True),
        ["omit_pg_attver=false", "no_comment_meta_restrict"],
    ) == TweakOptions(omit_pg_attver=False, comment_meta_restrict=False)
    assert apply_tweak_overrides(
        TweakOptions(omit_pg_attver=True),
        ["no-omit_pg_attver", "comment_meta_restrict=true"],
    ) == TweakOptions(omit_pg_attver=False, comment_meta_restrict=True)

    with pytest.raises(ConfigError, match="Empty tweak name"):
        apply_tweak_overrides(base, [""])
    with pytest.raises(ConfigError, match="Empty tweak name"):
        apply_tweak_overrides(base, ["omit_pg_attver,"])
    with pytest.raises(ConfigError, match="Invalid boolean value"):
        apply_tweak_overrides(base, ["omit_pg_attver=maybe"])
    with pytest.raises(ConfigError, match="Unknown tweak"):
        apply_tweak_overrides(base, ["unknown_tweak"])


def test_omit_pg_attver_removes_pure_and_partial_statements() -> None:
    assert omit_pg_attver_from_sql("") == ""
    assert omit_pg_attver_from_sql("   \n ") == "   \n "
    assert omit_pg_attver_from_sql("-- comment only\n") == "-- comment only\n"

    pure_set = (
        'ALTER TABLE IF EXISTS "public"."users"\n'
        '    ALTER COLUMN "name"\n'
        "    SET (pg_attver=2811141736);\n"
    )
    assert omit_pg_attver_from_sql(pure_set) == ""

    pure_reset = (
        'ALTER TABLE IF EXISTS "public"."users"\n'
        '    ALTER COLUMN "name"\n'
        "    RESET (pg_attver);\n"
    )
    assert omit_pg_attver_from_sql(pure_reset) == ""

    foreign_tbl = (
        'ALTER FOREIGN TABLE IF EXISTS "public"."ext_users"\n'
        '    ALTER COLUMN "col"\n'
        "    SET (pg_attver=12345);\n"
    )
    assert omit_pg_attver_from_sql(foreign_tbl) == ""

    quoted_upper = 'ALTER TABLE public.users ALTER COLUMN name SET ("PG_ATTVER"=99);\n'
    assert omit_pg_attver_from_sql(quoted_upper) == ""

    mixed_options = (
        "-- Keep comment\n"
        "ALTER TABLE IF EXISTS public.users\n"
        "    ALTER COLUMN name SET (n_distinct = 100, pg_attver = 2811141736);\n"
    )
    res_mixed = omit_pg_attver_from_sql(mixed_options)
    assert res_mixed.startswith("-- Keep comment\n")
    assert "n_distinct = 100" in res_mixed
    assert "pg_attver" not in res_mixed
    assert res_mixed.endswith(";\n")

    mixed_reset = "ALTER TABLE public.users ALTER COLUMN name RESET (pg_attver, n_distinct);"
    res_reset = omit_pg_attver_from_sql(mixed_reset)
    assert "RESET (n_distinct)" in res_reset
    assert "pg_attver" not in res_reset

    multi_cmd = (
        "ALTER TABLE public.users\n"
        "    ADD COLUMN age int,\n"
        "    ALTER COLUMN name SET (pg_attver=2811141736);"
    )
    res_multi_cmd = omit_pg_attver_from_sql(multi_cmd)
    assert "ADD COLUMN age integer" in res_multi_cmd
    assert "pg_attver" not in res_multi_cmd

    no_semicolon = "ALTER TABLE public.users ALTER COLUMN name SET (pg_attver=1)"
    assert omit_pg_attver_from_sql(no_semicolon) == ""


def test_omit_pg_attver_preserves_other_ddl_and_comments() -> None:
    ddl = (
        "-- 日本語コメント 😀\n"
        "CREATE TABLE IF NOT EXISTS public.users (\n"
        "    id integer NOT NULL,\n"
        "    name text\n"
        ");\n\n"
        "ALTER TABLE IF EXISTS public.users\n"
        "    ALTER COLUMN name\n"
        "    SET (pg_attver=2811141736);\n\n"
        "COMMENT ON TABLE public.users IS 'ALTER TABLE t ALTER COLUMN c SET (pg_attver=1);';\n"
    )
    cleaned = omit_pg_attver_from_sql(ddl)
    expected = (
        "-- 日本語コメント 😀\n"
        "CREATE TABLE IF NOT EXISTS public.users (\n"
        "    id integer NOT NULL,\n"
        "    name text\n"
        ");\n\n"
        "COMMENT ON TABLE public.users IS 'ALTER TABLE t ALTER COLUMN c SET (pg_attver=1);';\n"
    )
    assert cleaned == expected

    # Table-level reloptions and non-pg_attver column options must stay verbatim.
    untouched = (
        "ALTER TABLE public.users SET (fillfactor = 80);\n"
        "ALTER TABLE public.users ALTER COLUMN name SET (n_distinct = 50);\n"
        "SELECT 1;\n"
    )
    assert omit_pg_attver_from_sql(untouched) == untouched

    inline_comment_tail = (
        "ALTER TABLE public.users ALTER COLUMN name SET (pg_attver=1); -- keep note\n"
        "SELECT 1;"
    )
    cleaned_inline = omit_pg_attver_from_sql(inline_comment_tail)
    assert "-- keep note" in cleaned_inline
    assert "pg_attver" not in cleaned_inline

    with pytest.raises(SqlTweakError, match="Failed to parse SQL"):
        omit_pg_attver_from_sql("ALTER TABLE (;")


def test_comment_meta_restrict_in_sql() -> None:
    assert comment_meta_restrict_in_sql("") == ""

    no_restrict = "-- PostgreSQL database dump\nCREATE TABLE public.t (id int);\n"
    assert comment_meta_restrict_in_sql(no_restrict) == no_restrict

    dump_text = (
        "--\r\n"
        "-- PostgreSQL database dump (日本語コメント)\r\n"
        "--\r\n\r\n"
        "\\restrict 7abcBBCp7zbKcyo3Tmatf2SNpbDZFgtnZWSNp9HdCDRteaqBTZ5l9FSZ3It6od1V\r\n\r\n"
        "CREATE FUNCTION public.demo() RETURNS text LANGUAGE sql AS $$\r\n"
        "SELECT '\r\n"
        "\\restrict fake_inside_dollar_and_string';\r\n"
        "$$;\r\n"
        "SELECT '\r\n"
        "\\restrict fake_single_line_string';\r\n"
        'CREATE TABLE "\r\n'
        '\\restrict fake_ident" (id int);\r\n'
        "/*\r\n"
        "\\unrestrict fake_block_comment\r\n"
        "*/\r\n"
        "-- \\restrict already_commented\r\n"
        "  \\unrestrict 7abcBBCp7zbKcyo3Tmatf2SNpbDZFgtnZWSNp9HdCDRteaqBTZ5l9FSZ3It6od1V\r\n"
    )
    result = comment_meta_restrict_in_sql(dump_text)

    assert (
        "-- \\restrict 7abcBBCp7zbKcyo3Tmatf2SNpbDZFgtnZWSNp9HdCDRteaqBTZ5l9FSZ3It6od1V\r\n"
        in result
    )
    assert (
        "--   \\unrestrict 7abcBBCp7zbKcyo3Tmatf2SNpbDZFgtnZWSNp9HdCDRteaqBTZ5l9FSZ3It6od1V\r\n"
        in result
    )
    assert "\n\\restrict fake_inside_dollar_and_string';\r\n" in result
    assert "\n\\restrict fake_single_line_string';\r\n" in result
    assert '\n\\restrict fake_ident" (id int);\r\n' in result
    assert "\n\\unrestrict fake_block_comment\r\n" in result
    assert "\n-- \\restrict already_commented\r\n" in result

    with pytest.raises(SqlTweakError, match="Failed to scan SQL"):
        comment_meta_restrict_in_sql("\\restrict key\nSELECT 'unterminated;\n")
