"""Unit tests for the high-level client and CLI entrypoint."""

from __future__ import annotations

import json
from pathlib import Path
import threading

import click
from click.testing import CliRunner
import pytest

from pgadmin_schemadiff import (
    DEFAULT_FULL_DUMP_FILENAME,
    ApplyResult,
    Config,
    DatabaseConfig,
    DatabaseError,
    DDLSourceError,
    DiffItem,
    DiffStatus,
    OutputFilterOptions,
    PgDumpError,
    SchemaDiffClient,
    TweakOptions,
    cli,
    run_schema_diff,
)


@pytest.fixture(autouse=True)
def mock_psql_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pgadmin_schemadiff.client.resolve_psql", lambda *_args: Path("psql"))


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
        "pgadmin_schemadiff.client.prepare_database_files",
        lambda db_cfg, scripts, *, psql: prepared.append((db_cfg.database, [s.name for s in scripts])),
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
    assert sorted(prepared) == [("db_new", ["new.sql"]), ("db_old", ["old.sql"])]
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

    monkeypatch.setattr("pgadmin_schemadiff.client.prepare_database_files", lambda _db, _sc, *, psql: None)
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

    def raise_ddl_err(
        self: SchemaDiffClient,
        _old: object,
        _new: object,
        **_kwargs: object,
    ) -> object:
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


def test_client_apply_and_full_dump_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_path = _write_sample_config(tmp_path)
    old_sql = tmp_path / "old.sql"
    new_sql = tmp_path / "new.sql"
    old_sql.write_text("CREATE TABLE a (id int);", encoding="utf-8")
    new_sql.write_text("CREATE TABLE b (id int);", encoding="utf-8")

    monkeypatch.setattr("pgadmin_schemadiff.client.prepare_database_files", lambda _db, _sc, *, psql: None)
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

    events: list[tuple[str, object]] = []

    def fake_apply_diff_script(db_cfg: DatabaseConfig, script: str) -> None:
        events.append(("apply", db_cfg.database, script))

    def fake_run_pg_dump_schema(_cfg: Config, output_path: str | Path) -> Path:
        if isinstance(output_path, str) and not output_path.strip():
            raise PgDumpError("Full dump output path must not be empty.")
        events.append(("dump", output_path))
        return Path(output_path)

    monkeypatch.setattr("pgadmin_schemadiff.client.apply_diff_script", fake_apply_diff_script)
    monkeypatch.setattr("pgadmin_schemadiff.client.run_pg_dump_schema", fake_run_pg_dump_schema)

    res_both = run_schema_diff(cfg_path, old_sql, new_sql, apply=True, full_dump=True)
    assert res_both.full_dump_path == Path(DEFAULT_FULL_DUMP_FILENAME)
    assert res_both.apply_result == ApplyResult(succeeded=True)
    # Full dump must run before apply, and apply must target Old DB (db_old).
    assert events[0] == ("dump", DEFAULT_FULL_DUMP_FILENAME)
    assert events[1][0] == "apply"
    assert events[1][1] == "db_old"
    assert "CREATE TABLE b (id int);" in str(events[1][2])

    events.clear()
    custom_dump = tmp_path / "custom.sql"
    res_custom = run_schema_diff(cfg_path, old_sql, new_sql, full_dump=custom_dump)
    assert res_custom.full_dump_path == custom_dump
    assert res_custom.apply_result is None
    assert events == [("dump", custom_dump)]

    def fail_apply(_db_cfg: DatabaseConfig, _script: str) -> None:
        raise DatabaseError("relation already exists")

    monkeypatch.setattr("pgadmin_schemadiff.client.apply_diff_script", fail_apply)
    res_fail = run_schema_diff(
        cfg_path, old_sql, new_sql, apply=True, full_dump="dump_before_fail.sql"
    )
    assert res_fail.full_dump_path == Path("dump_before_fail.sql")
    assert res_fail.apply_result == ApplyResult(
        succeeded=False, error="relation already exists"
    )

    with pytest.raises(PgDumpError, match="must not be empty"):
        run_schema_diff(cfg_path, old_sql, new_sql, full_dump="")


def test_cli_apply_and_full_dump_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg_path = _write_sample_config(tmp_path)
    old_sql = tmp_path / "old.sql"
    new_sql = tmp_path / "new.sql"
    old_sql.write_text("CREATE TABLE a (id int);", encoding="utf-8")
    new_sql.write_text("CREATE TABLE b (id int);", encoding="utf-8")

    monkeypatch.setattr("pgadmin_schemadiff.client.prepare_database_files", lambda _db, _sc, *, psql: None)
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

    applied_scripts: list[str] = []
    dumped_paths: list[str | Path] = []

    monkeypatch.setattr(
        "pgadmin_schemadiff.client.apply_diff_script",
        lambda _db, script: applied_scripts.append(script),
    )
    monkeypatch.setattr(
        "pgadmin_schemadiff.client.run_pg_dump_schema",
        lambda _cfg, out_path: dumped_paths.append(out_path) or Path(out_path),
    )

    rc_apply_ok = cli.main(
        ["-c", str(cfg_path), "--old", str(old_sql), "--new", str(new_sql), "--apply"]
    )
    assert rc_apply_ok == 0
    captured = capsys.readouterr()
    assert "CREATE TABLE b (id int);" in captured.out
    assert "Error:" not in captured.err
    for step_prefix in ("[1/5]", "[2/5]", "[3/5]", "[4/5]", "[5/5]", "Done."):
        assert step_prefix in captured.err
    assert len(applied_scripts) == 1

    rc_dump_default = cli.main(
        ["-c", str(cfg_path), "--old", str(old_sql), "--new", str(new_sql), "--full-dump"]
    )
    assert rc_dump_default == 0
    assert capsys.readouterr().out
    assert dumped_paths[-1] == DEFAULT_FULL_DUMP_FILENAME

    rc_dump_and_apply = cli.main(
        [
            "-c",
            str(cfg_path),
            "--old",
            str(old_sql),
            "--new",
            str(new_sql),
            "--full-dump",
            "--apply",
        ]
    )
    assert rc_dump_and_apply == 0
    assert capsys.readouterr().out
    assert dumped_paths[-1] == DEFAULT_FULL_DUMP_FILENAME

    custom_out = str(tmp_path / "custom_dump.sql")
    rc_dump_named = cli.main(
        [
            "-c",
            str(cfg_path),
            "--old",
            str(old_sql),
            "--new",
            str(new_sql),
            "--full-dump",
            custom_out,
        ]
    )
    assert rc_dump_named == 0
    assert capsys.readouterr().out
    assert dumped_paths[-1] == custom_out

    eq_out = str(tmp_path / "eq_dump.sql")
    rc_dump_eq = cli.main(
        [
            "-c",
            str(cfg_path),
            "--old",
            str(old_sql),
            "--new",
            str(new_sql),
            f"--full-dump={eq_out}",
            "--format",
            "json",
        ]
    )
    assert rc_dump_eq == 0
    json_out = json.loads(capsys.readouterr().out)
    assert json_out["full_dump_path"] == eq_out

    def fail_apply(_db: DatabaseConfig, _script: str) -> None:
        raise DatabaseError("cannot alter table")

    monkeypatch.setattr("pgadmin_schemadiff.client.apply_diff_script", fail_apply)

    rc_apply_fail_sql = cli.main(
        ["-c", str(cfg_path), "--old", str(old_sql), "--new", str(new_sql), "--apply"]
    )
    assert rc_apply_fail_sql == 1
    cap_fail_sql = capsys.readouterr()
    assert "CREATE TABLE b (id int);" in cap_fail_sql.out
    assert "Error: cannot alter table" in cap_fail_sql.err

    rc_apply_fail_json = cli.main(
        [
            "-c",
            str(cfg_path),
            "--old",
            str(old_sql),
            "--new",
            str(new_sql),
            "--apply",
            "--format",
            "json",
        ]
    )
    assert rc_apply_fail_json == 1
    cap_fail_json = capsys.readouterr()
    parsed_fail = json.loads(cap_fail_json.out)
    assert parsed_fail["apply"] == {"succeeded": False, "error": "cannot alter table"}
    assert "Error: cannot alter table" in cap_fail_json.err

    def fail_dump(_cfg: Config, _out: str | Path) -> Path:
        raise PgDumpError("pg_dump failed")

    monkeypatch.setattr("pgadmin_schemadiff.client.run_pg_dump_schema", fail_dump)
    rc_dump_err = cli.main(
        ["-c", str(cfg_path), "--old", str(old_sql), "--new", str(new_sql), "--full-dump"]
    )
    assert rc_dump_err == 1
    cap_dump_err = capsys.readouterr()
    assert cap_dump_err.out == ""
    assert "Error: pg_dump failed" in cap_dump_err.err


def test_tweak_omit_pg_attver_and_cli_tweak_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg_path = tmp_path / "config_tweak.toml"
    cfg_path.write_text(
        """
[pgadmin]
install_path = "C:/pgAdmin4"

[tweak]
omit_pg_attver = true
comment_meta_restrict = true

[databases.old]
host = "localhost"
database = "db_old"
user = "postgres"

[databases.new]
host = "localhost"
database = "db_new"
user = "postgres"
""",
        encoding="utf-8",
    )
    old_sql = tmp_path / "old.sql"
    new_sql = tmp_path / "new.sql"
    old_sql.write_text("CREATE TABLE a (id int);", encoding="utf-8")
    new_sql.write_text("CREATE TABLE b (id int);", encoding="utf-8")

    monkeypatch.setattr("pgadmin_schemadiff.client.prepare_database_files", lambda _db, _sc, *, psql: None)
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
                diff_ddl=(
                    "CREATE TABLE public.b (id int);\n\n"
                    "ALTER TABLE IF EXISTS public.b ALTER COLUMN id SET (pg_attver=2811141736);\n"
                ),
            ),
            DiffItem(
                id=2,
                node_type="table",
                label="Tables",
                title="c_attver_only",
                status=DiffStatus.DIFFERENT,
                oid=20,
                diff_ddl="ALTER TABLE IF EXISTS public.c ALTER COLUMN x RESET (pg_attver);\n",
            ),
        ],
    )

    applied: list[str] = []
    seen_tweaks: list[TweakOptions] = []
    monkeypatch.setattr(
        "pgadmin_schemadiff.client.apply_diff_script",
        lambda _db, script: applied.append(script),
    )
    monkeypatch.setattr(
        "pgadmin_schemadiff.client.run_pg_dump_schema",
        lambda cfg, out_path: seen_tweaks.append(cfg.tweak) or Path(out_path),
    )

    # Config enables both tweaks; --apply must receive SQL without pg_attver
    rc = cli.main(
        ["-c", str(cfg_path), "--old", str(old_sql), "--new", str(new_sql), "--apply", "--full-dump"]
    )
    assert rc == 0
    out_sql = capsys.readouterr().out
    assert "CREATE TABLE public.b (id int);" in out_sql
    assert "pg_attver" not in out_sql
    assert "pg_attver" not in applied[0]
    assert seen_tweaks[-1] == TweakOptions(omit_pg_attver=True, comment_meta_restrict=True)

    # CLI can override config [tweak] values to false
    rc_override = cli.main(
        [
            "-c",
            str(cfg_path),
            "--old",
            str(old_sql),
            "--new",
            str(new_sql),
            "--full-dump",
            "--tweak",
            "omit_pg_attver=false,no_comment_meta_restrict",
        ]
    )
    assert rc_override == 0
    out_raw = capsys.readouterr().out
    assert "pg_attver=2811141736" in out_raw
    assert seen_tweaks[-1] == TweakOptions(omit_pg_attver=False, comment_meta_restrict=False)

    # Invalid tweak name returns exit code 2
    rc_bad_tweak = cli.main(
        ["-c", str(cfg_path), "--old", str(old_sql), "--new", str(new_sql), "--tweak", "bogus"]
    )
    assert rc_bad_tweak == 2
    assert "Configuration error:" in capsys.readouterr().err


def test_click_cli_runner_and_abort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg_path = _write_sample_config(tmp_path)
    old_sql = tmp_path / "old.sql"
    new_sql = tmp_path / "new.sql"
    old_sql.write_text("CREATE TABLE a (id int);", encoding="utf-8")
    new_sql.write_text("CREATE TABLE b (id int);", encoding="utf-8")

    runner = CliRunner()

    for help_flag in ("--help", "-h"):
        help_res = runner.invoke(cli.cli, [help_flag])
        assert help_res.exit_code == 0
        assert "--config" in help_res.output
        assert "--apply" in help_res.output
        assert "--full-dump" in help_res.output
        assert "--tweak" in help_res.output

    monkeypatch.setattr("pgadmin_schemadiff.client.prepare_database_files", lambda _db, _sc, *, psql: None)
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
                diff_ddl=(
                    "CREATE TABLE b (id int);\n\n"
                    "ALTER TABLE IF EXISTS b ALTER COLUMN id SET (pg_attver=123);\n"
                ),
            )
        ],
    )
    applied: list[str] = []
    dumped: list[str | Path] = []
    monkeypatch.setattr(
        "pgadmin_schemadiff.client.apply_diff_script",
        lambda _db, script: applied.append(script),
    )
    monkeypatch.setattr(
        "pgadmin_schemadiff.client.run_pg_dump_schema",
        lambda _cfg, out_path: dumped.append(out_path) or Path(out_path),
    )

    res_sql = runner.invoke(
        cli.cli,
        [
            "-c",
            str(cfg_path),
            "--old",
            str(old_sql),
            "--new",
            str(new_sql),
            "--full-dump",
            "--apply",
            "--tweak",
            "omit_pg_attver",
        ],
    )
    assert res_sql.exit_code == 0
    assert "CREATE TABLE b (id int);" in res_sql.stdout
    assert "pg_attver" not in res_sql.stdout
    assert dumped[-1] == DEFAULT_FULL_DUMP_FILENAME
    assert len(applied) == 1

    custom_dump = str(tmp_path / "runner_dump.sql")
    res_json = runner.invoke(
        cli.cli,
        [
            "-c",
            str(cfg_path),
            "--old",
            str(old_sql),
            "--new",
            str(new_sql),
            "--full-dump",
            custom_dump,
            "--format",
            "json",
        ],
    )
    assert res_json.exit_code == 0
    parsed = json.loads(res_json.stdout)
    assert parsed["full_dump_path"] == custom_dump

    res_cfg_err = runner.invoke(
        cli.cli,
        ["-c", str(tmp_path / "missing.toml"), "--old", str(old_sql), "--new", str(new_sql)],
    )
    assert res_cfg_err.exit_code == 2
    assert "Configuration error:" in res_cfg_err.stderr

    monkeypatch.setattr(
        "pgadmin_schemadiff.client.apply_diff_script",
        lambda _db, _sc: (_ for _ in ()).throw(DatabaseError("apply boom")),
    )
    res_apply_err = runner.invoke(
        cli.cli,
        ["-c", str(cfg_path), "--old", str(old_sql), "--new", str(new_sql), "--apply"],
    )
    assert res_apply_err.exit_code == 1
    assert "CREATE TABLE b (id int);" in res_apply_err.stdout
    assert "Error: apply boom" in res_apply_err.stderr

    def raise_abort(*_args: object, **_kwargs: object) -> None:
        raise click.Abort()

    monkeypatch.setattr(cli.cli, "main", raise_abort)
    assert cli.main([]) == 1
    assert "Aborted!" in capsys.readouterr().err


def test_progress_reporting_client_and_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg_path = _write_sample_config(tmp_path)
    old_sql = tmp_path / "old.sql"
    new_sql = tmp_path / "new.sql"
    old_sql.write_text("CREATE TABLE a (id int);", encoding="utf-8")
    new_sql.write_text("CREATE TABLE b (id int);", encoding="utf-8")

    monkeypatch.setattr("pgadmin_schemadiff.client.prepare_database_files", lambda _db, _sc, *, psql: None)
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
    monkeypatch.setattr(
        "pgadmin_schemadiff.client.apply_diff_script",
        lambda _db, _script: None,
    )
    monkeypatch.setattr(
        "pgadmin_schemadiff.client.run_pg_dump_schema",
        lambda _cfg, out_path: Path(out_path),
    )

    # 1. run_schema_diff standard (4 steps, with Old/New prepared in parallel during step 2)
    events_std: list[str] = []
    run_schema_diff(cfg_path, old_sql, new_sql, progress=events_std.append)
    step_lines = [line for line in events_std if line.startswith("[")]
    assert [line.split(" ", 1)[0] for line in step_lines] == [
        "[1/4]",
        "[2/4]",
        "[3/4]",
        "[4/4]",
    ]
    assert "Loading DDL inputs" in step_lines[0]
    assert "Preparing Target (Old) database 'db_old'" in step_lines[1]
    assert "Source (New) database 'db_new'" in step_lines[1]
    assert "in parallel" in step_lines[1]
    assert "Running pgAdmin 4 Schema Diff" in step_lines[2]
    assert "Generating diff script" in step_lines[3]

    idx_step2 = events_std.index(step_lines[1])
    idx_step3 = events_std.index(step_lines[2])
    assert set(events_std[idx_step2 + 1 : idx_step3]) == {
        "  - Target (Old) database 'db_old' (schema 'public') prepared.",
        "  - Source (New) database 'db_new' (schema 'public') prepared.",
    }

    # 2. run_schema_diff with full_dump + apply (6 steps)
    events_full: list[str] = []
    run_schema_diff(
        cfg_path, old_sql, new_sql, apply=True, full_dump=True, progress=events_full.append
    )
    step_lines_full = [line for line in events_full if line.startswith("[")]
    assert [line.split(" ", 1)[0] for line in step_lines_full] == [
        "[1/6]",
        "[2/6]",
        "[3/6]",
        "[4/6]",
        "[5/6]",
        "[6/6]",
    ]
    assert "Creating schema-only full dump" in step_lines_full[4]
    assert "Applying diff script to Target (Old) database 'db_old'" in step_lines_full[5]

    # 3. diff_existing directly (2 steps and 4 steps)
    cfg = Config(
        pgadmin_path=Path("/pgadmin"),
        old_db=DatabaseConfig(host="h1", database="old_d", user="u", search_path="s_old"),
        new_db=DatabaseConfig(host="h2", database="new_d", user="u", search_path="s_new"),
    )
    events_existing: list[str] = []
    SchemaDiffClient(cfg, progress=events_existing.append).diff_existing()
    assert [line.split(" ", 1)[0] for line in events_existing] == ["[1/2]", "[2/2]"]

    events_existing_all: list[str] = []
    SchemaDiffClient(cfg, progress=events_existing_all.append).diff_existing(
        apply=True, full_dump="out.sql"
    )
    assert [line.split(" ", 1)[0] for line in events_existing_all] == [
        "[1/4]",
        "[2/4]",
        "[3/4]",
        "[4/4]",
    ]

    # 4. CLI stdout vs stderr separation via CliRunner
    runner = CliRunner()
    res_json = runner.invoke(
        cli.cli,
        ["-c", str(cfg_path), "--old", str(old_sql), "--new", str(new_sql), "--format", "json"],
    )
    assert res_json.exit_code == 0
    parsed = json.loads(res_json.stdout)
    assert parsed["summary"]["source_only"] == 1
    assert "[1/4]" not in res_json.stdout
    err_lines = res_json.stderr.strip().splitlines()
    assert [line.split(" ", 1)[0] for line in err_lines if line.startswith("[") or line == "Done."] == [
        "[1/4]",
        "[2/4]",
        "[3/4]",
        "[4/4]",
        "Done.",
    ]
    assert "  - Target (Old) database 'db_old' (schema 'public') prepared." in err_lines
    assert "  - Source (New) database 'db_new' (schema 'public') prepared." in err_lines

    res_both = runner.invoke(
        cli.cli,
        [
            "-c",
            str(cfg_path),
            "--old",
            str(old_sql),
            "--new",
            str(new_sql),
            "--full-dump",
            "--apply",
        ],
    )
    assert res_both.exit_code == 0
    assert "[1/6]" not in res_both.stdout
    assert "CREATE TABLE b (id int);" in res_both.stdout
    assert [
        line.split(" ", 1)[0]
        for line in res_both.stderr.strip().splitlines()
        if line.startswith("[") or line == "Done."
    ] == [
        "[1/6]",
        "[2/6]",
        "[3/6]",
        "[4/6]",
        "[5/6]",
        "[6/6]",
        "Done.",
    ]

    # 5. Apply failure prints steps + Error, does NOT print Done., keeps stdout intact
    monkeypatch.setattr(
        "pgadmin_schemadiff.client.apply_diff_script",
        lambda _db, _sc: (_ for _ in ()).throw(DatabaseError("apply failed")),
    )
    res_fail = runner.invoke(
        cli.cli,
        ["-c", str(cfg_path), "--old", str(old_sql), "--new", str(new_sql), "--apply"],
    )
    assert res_fail.exit_code == 1
    assert "CREATE TABLE b (id int);" in res_fail.stdout
    assert "[5/5]" in res_fail.stderr
    assert "Error: apply failed" in res_fail.stderr
    assert "Done." not in res_fail.stderr

    # 6. ConfigError emits no progress steps
    capsys.readouterr()
    rc_cfg = cli.main(
        ["-c", str(tmp_path / "missing.toml"), "--old", str(old_sql), "--new", str(new_sql)]
    )
    assert rc_cfg == 2
    cfg_err = capsys.readouterr().err
    assert "Configuration error:" in cfg_err
    assert "[1/" not in cfg_err


def test_diff_ddls_prepares_old_and_new_in_parallel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_path = _write_sample_config(tmp_path)
    old_sql = tmp_path / "old.sql"
    new_sql = tmp_path / "new.sql"
    old_sql.write_text("CREATE TABLE a (id int);", encoding="utf-8")
    new_sql.write_text("CREATE TABLE b (id int);", encoding="utf-8")

    monkeypatch.setattr(
        "pgadmin_schemadiff.client.run_pgadmin_diff",
        lambda _cfg, include_identical_ddl=False: [],
    )

    barrier = threading.Barrier(2, timeout=5)
    seen_threads: dict[str, int] = {}

    def concurrent_prepare(db_cfg: DatabaseConfig, _scripts: object, *, psql: object) -> None:
        seen_threads[db_cfg.database] = threading.get_ident()
        barrier.wait()

    monkeypatch.setattr("pgadmin_schemadiff.client.prepare_database_files", concurrent_prepare)
    run_schema_diff(cfg_path, old_sql, new_sql)
    assert set(seen_threads.keys()) == {"db_old", "db_new"}
    assert seen_threads["db_old"] != seen_threads["db_new"]

    def fail_old(db_cfg: DatabaseConfig, _scripts: object, *, psql: object) -> None:
        if db_cfg.database == "db_old":
            raise DatabaseError("old db boom")

    monkeypatch.setattr("pgadmin_schemadiff.client.prepare_database_files", fail_old)
    with pytest.raises(DatabaseError, match="old db boom"):
        run_schema_diff(cfg_path, old_sql, new_sql)

    def fail_new(db_cfg: DatabaseConfig, _scripts: object, *, psql: object) -> None:
        if db_cfg.database == "db_new":
            raise DatabaseError("new db boom")

    monkeypatch.setattr("pgadmin_schemadiff.client.prepare_database_files", fail_new)
    with pytest.raises(DatabaseError, match="new db boom"):
        run_schema_diff(cfg_path, old_sql, new_sql)
