"""Unit tests for the high-level client and CLI entrypoint."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pgadmin_schemadiff import (
    Config,
    DatabaseConfig,
    DDLSourceError,
    DiffItem,
    DiffStatus,
    OutputFilterOptions,
    SchemaDiffClient,
    cli,
    run_schema_diff,
)


def _write_sample_config(tmp_path: Path) -> Path:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        """
[pgadmin]
install_path = "C:/pgAdmin4"

[filter]
source_only = true
target_only = true
different = true
same = false

[databases.old]
host = "localhost"
port = 5432
database = "db_old"
user = "postgres"
search_path = "public"

[databases.new]
host = "localhost"
port = 5432
database = "db_new"
user = "postgres"
search_path = "public"
""",
        encoding="utf-8",
    )
    return cfg_path


def test_client_diff_ddls_and_run_schema_diff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_path = _write_sample_config(tmp_path)
    old_sql = tmp_path / "old.sql"
    new_sql = tmp_path / "new.sql"
    old_sql.write_text("CREATE TABLE a (id int);", encoding="utf-8")
    new_sql.write_text("CREATE TABLE a (id int); CREATE TABLE b (id int);", encoding="utf-8")

    prepared: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        "pgadmin_schemadiff.client.prepare_database",
        lambda db_cfg, scripts: prepared.append((db_cfg.database, [s.name for s in scripts])),
    )
    monkeypatch.setattr(
        "pgadmin_schemadiff.client.run_pgadmin_diff",
        lambda _cfg, include_identical_ddl=False: [
            DiffItem(
                id=1,
                node_type="table",
                label="Tables",
                title="a",
                status=DiffStatus.IDENTICAL,
                oid=1,
            ),
            DiffItem(
                id=2,
                node_type="table",
                label="Tables",
                title="b",
                status=DiffStatus.SOURCE_ONLY,
                oid=2,
                diff_ddl="CREATE TABLE b (id int);",
            ),
        ],
    )

    result = run_schema_diff(cfg_path, old_sql, new_sql)
    assert prepared == [("db_old", ["old.sql"]), ("db_new", ["new.sql"])]
    assert [i.title for i in result.items] == ["b"]
    assert len(result.all_items) == 2
    assert "CREATE TABLE b (id int);" in result.script


def test_cli_sql_json_and_error_exit_codes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg_path = _write_sample_config(tmp_path)
    old_sql = tmp_path / "old.sql"
    new_sql = tmp_path / "new.sql"
    old_sql.write_text("CREATE TABLE a (id int);", encoding="utf-8")
    new_sql.write_text("CREATE TABLE b (id int);", encoding="utf-8")

    monkeypatch.setattr("pgadmin_schemadiff.client.prepare_database", lambda _db, _sc: None)
    monkeypatch.setattr(
        "pgadmin_schemadiff.client.run_pgadmin_diff",
        lambda _cfg, include_identical_ddl=False: [
            DiffItem(
                id=1,
                node_type="table",
                label="Tables",
                title="b",
                status=DiffStatus.SOURCE_ONLY,
                oid=10,
                diff_ddl="CREATE TABLE b (id int);",
            )
        ],
    )

    rc_sql = cli.main(["--config", str(cfg_path), "--old", str(old_sql), "--new", str(new_sql)])
    assert rc_sql == 0
    out_sql = capsys.readouterr().out
    assert "BEGIN;" in out_sql
    assert "CREATE TABLE b (id int);" in out_sql

    rc_json = cli.main(
        ["-c", str(cfg_path), "--old", str(old_sql), "--new", str(new_sql), "--format", "json"]
    )
    assert rc_json == 0
    out_json = json.loads(capsys.readouterr().out)
    assert out_json["summary"]["source_only"] == 1
    assert out_json["items"][0]["title"] == "b"

    rc_cfg_err = cli.main(
        ["-c", str(tmp_path / "missing.toml"), "--old", str(old_sql), "--new", str(new_sql)]
    )
    assert rc_cfg_err == 2
    assert "Configuration error:" in capsys.readouterr().err

    def raise_ddl_err(self: SchemaDiffClient, _old: object, _new: object) -> object:
        raise DDLSourceError("missing ddl file")

    monkeypatch.setattr(SchemaDiffClient, "diff_ddls", raise_ddl_err)
    rc_ddl_err = cli.main(["-c", str(cfg_path), "--old", str(old_sql), "--new", str(new_sql)])
    assert rc_ddl_err == 1
    assert "Error: missing ddl file" in capsys.readouterr().err

    with pytest.raises(SystemExit) as exc_info:
        cli.main([])
    assert exc_info.value.code == 2


def test_client_diff_existing_respects_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = Config(
        pgadmin_path=Path("/pgadmin"),
        old_db=DatabaseConfig(host="h1", user="u"),
        new_db=DatabaseConfig(host="h2", user="u"),
        output_filter=OutputFilterOptions(
            source_only=False, target_only=True, different=False, same=False
        ),
    )
    monkeypatch.setattr(
        "pgadmin_schemadiff.client.run_pgadmin_diff",
        lambda _cfg, include_identical_ddl=False: [
            DiffItem(
                id=1,
                node_type="table",
                label="Tables",
                title="only_new",
                status=DiffStatus.SOURCE_ONLY,
                diff_ddl="CREATE TABLE only_new();",
            ),
            DiffItem(
                id=2,
                node_type="table",
                label="Tables",
                title="only_old",
                status=DiffStatus.TARGET_ONLY,
                diff_ddl="DROP TABLE only_old;",
            ),
        ],
    )
    res = SchemaDiffClient(cfg).diff_existing()
    assert [i.title for i in res.items] == ["only_old"]
    assert "DROP TABLE only_old;" in res.script
    assert "CREATE TABLE only_new();" not in res.script
