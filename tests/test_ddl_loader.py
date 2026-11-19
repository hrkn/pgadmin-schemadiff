"""Unit tests for DDL loading from files, directories, and ZIP archives."""

from __future__ import annotations

from pathlib import Path
import zipfile

import pytest

from pgadmin_schemadiff.ddl_loader import DDLScript, load_ddl_scripts
from pgadmin_schemadiff.exceptions import DDLSourceError


def test_load_single_file_and_bom(tmp_path: Path) -> None:
    sql_file = tmp_path / "schema.sql"
    sql_file.write_bytes(b"\xef\xbb\xbfCREATE TABLE t (id int);")

    scripts = load_ddl_scripts(sql_file)
    assert scripts == [DDLScript(name="schema.sql", sql="CREATE TABLE t (id int);")]


def test_load_directory_non_recursive_sorted(tmp_path: Path) -> None:
    ddl_dir = tmp_path / "ddl"
    ddl_dir.mkdir()
    (ddl_dir / "02_second.sql").write_text("CREATE TABLE b (id int);", encoding="utf-8")
    (ddl_dir / "01_first.SQL").write_text("CREATE TABLE a (id int);", encoding="utf-8")
    (ddl_dir / "notes.txt").write_text("ignored", encoding="utf-8")
    (ddl_dir / "00_dir.sql").mkdir()  # Directory named *.sql must be ignored

    nested = ddl_dir / "nested"
    nested.mkdir()
    (nested / "99_nested.sql").write_text("CREATE TABLE nested (id int);", encoding="utf-8")

    scripts = load_ddl_scripts(ddl_dir)
    assert [s.name for s in scripts] == ["01_first.SQL", "02_second.sql"]
    assert scripts[0].sql == "CREATE TABLE a (id int);"
    assert scripts[1].sql == "CREATE TABLE b (id int);"


def test_load_empty_directory(tmp_path: Path) -> None:
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    assert load_ddl_scripts(empty_dir) == []


def test_load_zip_non_recursive_sorted(tmp_path: Path) -> None:
    zip_path = tmp_path / "bundle.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("02_views.sql", "CREATE VIEW v AS SELECT 1;")
        zf.writestr("01_tables.sql", "CREATE TABLE t (id int);")
        zf.writestr("README.md", "ignore me")
        zf.writestr("sub/03_nested.sql", "CREATE TABLE sub_t (id int);")

    scripts = load_ddl_scripts(zip_path)
    assert [s.name for s in scripts] == ["01_tables.sql", "02_views.sql"]


def test_load_zip_errors(tmp_path: Path) -> None:
    corrupt_zip = tmp_path / "bad.zip"
    corrupt_zip.write_bytes(b"not a zip")
    with pytest.raises(DDLSourceError, match="Failed to extract"):
        load_ddl_scripts(corrupt_zip)

    traversal_zip = tmp_path / "evil.zip"
    with zipfile.ZipFile(traversal_zip, "w") as zf:
        zf.writestr("../escape.sql", "DROP TABLE x;")
    with pytest.raises(DDLSourceError, match="path traversal"):
        load_ddl_scripts(traversal_zip)

    symlink_zip = tmp_path / "symlink.zip"
    with zipfile.ZipFile(symlink_zip, "w") as zf:
        info = zipfile.ZipInfo("link.sql")
        info.external_attr = 0o120777 << 16
        zf.writestr(info, "target.sql")
    with pytest.raises(DDLSourceError, match="symlink"):
        load_ddl_scripts(symlink_zip)


def test_load_missing_source_and_decode_error(tmp_path: Path) -> None:
    with pytest.raises(DDLSourceError, match="does not exist"):
        load_ddl_scripts(tmp_path / "nope.sql")

    bad_utf8 = tmp_path / "bad_encoding.sql"
    bad_utf8.write_bytes(b"\xff\xfe\x00\x80")
    with pytest.raises(DDLSourceError, match="Failed to read DDL file"):
        load_ddl_scripts(bad_utf8)
