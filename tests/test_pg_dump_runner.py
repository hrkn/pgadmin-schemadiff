"""Unit tests for pg_dump discovery and schema-only dump execution."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import types

import pytest

from pgadmin_schemadiff.config import Config, DatabaseConfig, IgnoreOptions
from pgadmin_schemadiff.exceptions import PgDumpError
from pgadmin_schemadiff.pg_dump_runner import (
    DEFAULT_FULL_DUMP_FILENAME,
    resolve_pg_dump,
    run_pg_dump_schema,
)


def _sample_config(
    pgadmin_path: Path,
    *,
    pg_dump_path: Path | None = None,
    ignore: IgnoreOptions | None = None,
    new_password: str = "pw_new",
    new_search_path: str = "new_sch",
) -> Config:
    return Config(
        pgadmin_path=pgadmin_path,
        old_db=DatabaseConfig(
            host="old-host",
            port=5432,
            database="old_db",
            user="old_user",
            password="pw_old",
            search_path="old_sch",
            sslmode="disable",
        ),
        new_db=DatabaseConfig(
            host="new-host",
            port=5433,
            database="new_db",
            user="new_user",
            password=new_password,
            search_path=new_search_path,
            sslmode="require",
        ),
        ignore=ignore or IgnoreOptions(),
        timeout_seconds=60,
        pg_dump_path=pg_dump_path,
    )


def test_resolve_pg_dump_variants_and_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    explicit_file = tmp_path / "custom" / "pg_dump.exe"
    explicit_file.parent.mkdir(parents=True)
    explicit_file.write_text("", encoding="utf-8")
    assert resolve_pg_dump(tmp_path / "pgadmin", explicit_file) == explicit_file

    explicit_dir = tmp_path / "bin_dir"
    explicit_dir.mkdir()
    dir_exe = explicit_dir / "pg_dump"
    dir_exe.write_text("", encoding="utf-8")
    assert resolve_pg_dump(tmp_path / "pgadmin", explicit_dir) == dir_exe

    empty_dir = tmp_path / "empty_dir"
    empty_dir.mkdir()
    with pytest.raises(PgDumpError, match="inside configured directory"):
        resolve_pg_dump(tmp_path / "pgadmin", empty_dir)

    with pytest.raises(PgDumpError, match="Configured pg_dump executable does not exist"):
        resolve_pg_dump(tmp_path / "pgadmin", tmp_path / "missing_pg_dump")

    monkeypatch.setattr(shutil, "which", lambda _name: None)

    win_pgadmin = tmp_path / "pg17" / "pgAdmin 4"
    win_dump = win_pgadmin / "runtime" / "pg_dump.exe"
    win_dump.parent.mkdir(parents=True)
    win_dump.write_text("", encoding="utf-8")
    assert resolve_pg_dump(win_pgadmin) == win_dump

    linux_pgadmin = tmp_path / "linux_pgadmin"
    linux_dump = linux_pgadmin / "runtime" / "pg_dump"
    linux_dump.parent.mkdir(parents=True)
    linux_dump.write_text("", encoding="utf-8")
    assert resolve_pg_dump(linux_pgadmin) == linux_dump

    mac_pgadmin = tmp_path / "pgAdmin 4.app"
    mac_dump = mac_pgadmin / "Contents" / "SharedSupport" / "pg_dump"
    mac_dump.parent.mkdir(parents=True)
    mac_dump.write_text("", encoding="utf-8")
    assert resolve_pg_dump(mac_pgadmin) == mac_dump

    sibling_root = tmp_path / "pg_root" / "pgAdmin 4"
    sibling_root.mkdir(parents=True)
    sibling_dump = tmp_path / "pg_root" / "bin" / "pg_dump.exe"
    sibling_dump.parent.mkdir(parents=True)
    sibling_dump.write_text("", encoding="utf-8")
    assert resolve_pg_dump(sibling_root) == sibling_dump

    isolated_root = tmp_path / "isolated"
    isolated_root.mkdir()
    monkeypatch.setattr(shutil, "which", lambda _name: str(tmp_path / "path_pg_dump"))
    assert resolve_pg_dump(isolated_root) == tmp_path / "path_pg_dump"

    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(PgDumpError, match="Could not locate 'pg_dump'"):
        resolve_pg_dump(isolated_root)


def test_resolve_local_pg_dump_smoke() -> None:
    local_install = Path(r"C:\Program Files\PostgreSQL\17\pgAdmin 4")
    if not local_install.exists():
        pytest.skip("pgAdmin 4 is not installed at default path")
    exe = resolve_pg_dump(local_install)
    assert exe.is_file()


def test_run_pg_dump_schema_command_env_and_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pgadmin_dir = tmp_path / "pgAdmin 4"
    pg_dump_exe = pgadmin_dir / "runtime" / "pg_dump.exe"
    pg_dump_exe.parent.mkdir(parents=True)
    pg_dump_exe.write_text("", encoding="utf-8")

    captured: dict[str, object] = {}

    def fake_run(cmd: list[str], **kwargs: object) -> types.SimpleNamespace:
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("PGPASSWORD", "stale_password")

    out_path = tmp_path / "nested" / "dumps" / DEFAULT_FULL_DUMP_FILENAME
    cfg_with_ignores = _sample_config(
        pgadmin_dir,
        ignore=IgnoreOptions(owner=True, whitespace=True, tablespace=True, grants=True),
        new_password="secret_new_pw",
        new_search_path='"$user", "App""Schema", public',
    )
    returned = run_pg_dump_schema(cfg_with_ignores, out_path)

    assert returned == out_path
    assert out_path.parent.is_dir()

    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    assert cmd[0] == str(pg_dump_exe)
    assert "--schema-only" in cmd
    assert "--format=plain" in cmd
    assert "--encoding=UTF8" in cmd
    assert "--no-password" in cmd
    assert "--strict-names" in cmd
    assert cmd[cmd.index("--host") + 1] == "new-host"
    assert cmd[cmd.index("--port") + 1] == "5433"
    assert cmd[cmd.index("--username") + 1] == "new_user"
    assert cmd[cmd.index("--dbname") + 1] == "new_db"
    assert cmd[cmd.index("--schema") + 1] == '"App""Schema"'
    assert cmd[cmd.index("--file") + 1] == str(out_path)
    assert "--no-owner" in cmd
    assert "--no-tablespaces" in cmd
    assert "--no-privileges" in cmd

    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["timeout"] == 60
    assert kwargs["errors"] == "replace"
    env = kwargs["env"]
    assert isinstance(env, dict)
    assert env["PGPASSWORD"] == "secret_new_pw"
    assert env["PGSSLMODE"] == "require"
    assert env["PATH"].split(os.pathsep)[0] == str(pg_dump_exe.parent)

    cfg_no_pw_no_ignore = _sample_config(
        pgadmin_dir,
        ignore=IgnoreOptions(owner=False, whitespace=False, tablespace=False, grants=False),
        new_password="",
    )
    out_plain = tmp_path / "plain.sql"
    run_pg_dump_schema(cfg_no_pw_no_ignore, out_plain)
    cmd2 = captured["cmd"]
    env2 = captured["kwargs"]["env"]  # type: ignore[index]
    assert isinstance(cmd2, list)
    assert "--no-owner" not in cmd2
    assert "--no-tablespaces" not in cmd2
    assert "--no-privileges" not in cmd2
    assert "PGPASSWORD" not in env2


def test_run_pg_dump_schema_error_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pgadmin_dir = tmp_path / "pgAdmin 4"
    pg_dump_exe = pgadmin_dir / "runtime" / "pg_dump.exe"
    pg_dump_exe.parent.mkdir(parents=True)
    pg_dump_exe.write_text("", encoding="utf-8")
    cfg = _sample_config(pgadmin_dir)

    with pytest.raises(PgDumpError, match="must not be empty"):
        run_pg_dump_schema(cfg, "   ")

    existing_dir = tmp_path / "a_dir"
    existing_dir.mkdir()
    with pytest.raises(PgDumpError, match="is a directory"):
        run_pg_dump_schema(cfg, existing_dir)

    def fail_mkdir(self: Path, *_args: object, **_kwargs: object) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "mkdir", fail_mkdir)
    with pytest.raises(PgDumpError, match="Failed to create parent directory"):
        run_pg_dump_schema(cfg, tmp_path / "new_parent" / "dump.sql")
    monkeypatch.undo()

    out_file = tmp_path / "out.sql"

    def timeout_run(cmd: list[str], **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=cfg.timeout_seconds)

    monkeypatch.setattr(subprocess, "run", timeout_run)
    with pytest.raises(PgDumpError, match="timed out"):
        run_pg_dump_schema(cfg, out_file)

    def oserr_run(*_args: object, **_kwargs: object) -> None:
        raise OSError("exec failed")

    monkeypatch.setattr(subprocess, "run", oserr_run)
    with pytest.raises(PgDumpError, match="Failed to launch pg_dump"):
        run_pg_dump_schema(cfg, out_file)

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_kw: types.SimpleNamespace(returncode=2, stdout="", stderr="connection refused"),
    )
    with pytest.raises(PgDumpError, match="connection refused"):
        run_pg_dump_schema(cfg, out_file)

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_kw: types.SimpleNamespace(returncode=3, stdout="fatal stdout", stderr=""),
    )
    with pytest.raises(PgDumpError, match="fatal stdout"):
        run_pg_dump_schema(cfg, out_file)
